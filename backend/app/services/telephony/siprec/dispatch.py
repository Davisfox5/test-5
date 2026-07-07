"""Production ``TranscriptionDispatch`` for SIPREC — Deepgram live sink.

The :class:`~backend.app.services.telephony.siprec.bridge.SiprecBridge`
normalises SRS audio frames and hands them to a
``TranscriptionDispatch``. Until now the only implementation was
``_NullDispatch`` (drops everything), so a deployed SRS produced
``SiprecSession`` rows but never a transcript — the last mile of the
SIPREC path was unwired.

This module closes that gap. It mirrors the Twilio media-stream path in
``api/telephony.py``:

* One Deepgram **live** WebSocket per SIP media stream (per SDP
  ``a=label``). SIPREC delivers each participant as its own stream, so
  one connection per label gives clean per-speaker attribution without
  relying on diarization of a mixed signal — ``label`` becomes the
  ``speaker`` on every segment.
* Each final transcript result is appended to the **same Redis live
  buffer** (``live:{live_session_id}:buffer``) that the browser and
  Twilio finalizers use, as ``{"text", "speaker", "timestamp"}`` JSON.
* ``close_session`` finalises that buffer through the shared
  :func:`_dispatch_batch_analysis`, which builds the ``Interaction`` and
  enqueues ``process_voice_interaction`` — so a SIPREC call lands as a
  fully analysed conversation exactly like every other channel.

The Deepgram SDK invokes result handlers on its own thread; the Redis
append is marshalled back onto the bridge's event loop via
``asyncio.run_coroutine_threadsafe`` (the buffer append is the only
shared mutation). Everything here is best-effort: a parse glitch or a
transient Deepgram error must never take down the SRS audio ingest path,
so handlers swallow-and-log.

Injectables (``connection_factory``, ``redis_client``, ``finalizer``)
exist so the dispatch is unit-testable without a live Deepgram or Celery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Mirror the browser path's safety-net TTL so a half-closed session can't
# leak a transcript into Redis forever (see api/websocket.py).
_LIVE_BUFFER_TTL_SECONDS = 6 * 60 * 60

ConnectionFactory = Callable[[], Any]
Finalizer = Callable[[Any, str], Awaitable[None]]


def _default_connection_factory(api_key: str) -> ConnectionFactory:
    def _make() -> Any:
        from deepgram import DeepgramClient  # imported lazily; heavy SDK

        return DeepgramClient(api_key).listen.live.v("1")

    return _make


async def _default_finalizer(redis: Any, session_id: str) -> None:
    # Late import — avoids a circular import (api.websocket imports models
    # + tasks) at module load and keeps the SDK-free test path clean.
    from backend.app.api.websocket import _dispatch_batch_analysis

    await _dispatch_batch_analysis(redis, session_id)


class _SessionSinks:
    """Per-recording-session state: one Deepgram connection per label."""

    def __init__(self, live_session_id: uuid.UUID, loop: asyncio.AbstractEventLoop):
        self.live_session_id = live_session_id
        self.buffer_key = f"live:{live_session_id}:buffer"
        self.loop = loop
        # label -> deepgram live connection
        self.connections: Dict[str, Any] = {}
        # Guards lazy per-label connection creation against concurrent
        # frames for the same (session, label).
        self.lock = asyncio.Lock()


class DeepgramSiprecDispatch:
    """Feed SIPREC audio into Deepgram live and persist the transcript.

    See module docstring. One instance per worker process (constructed
    in ``bridge.get_bridge``); it multiplexes every active recording
    session through a shared Redis connection.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        redis_client: Optional[Any] = None,
        connection_factory: Optional[ConnectionFactory] = None,
        finalizer: Optional[Finalizer] = None,
        model: str = "nova-3",
    ) -> None:
        self._model = model
        self._redis_client = redis_client
        self._finalizer = finalizer or _default_finalizer
        self._sessions: Dict[str, _SessionSinks] = {}
        self._lock = asyncio.Lock()
        self._sdk_missing_logged = False

        if connection_factory is not None:
            self._connection_factory: Optional[ConnectionFactory] = connection_factory
        else:
            key = api_key
            if key is None:
                from backend.app.config import get_settings

                key = get_settings().DEEPGRAM_API_KEY
            self._connection_factory = _default_connection_factory(key) if key else None

    # ── Redis ────────────────────────────────────────────────────────

    def _get_redis(self) -> Any:
        if self._redis_client is None:
            import redis.asyncio as aioredis

            from backend.app.config import get_settings

            self._redis_client = aioredis.from_url(
                get_settings().REDIS_URL, decode_responses=True
            )
        return self._redis_client

    # ── TranscriptionDispatch protocol ───────────────────────────────

    async def open_session(
        self,
        recording_session_id: str,
        live_session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        provider: str,
    ) -> None:
        """Register the session. Deepgram connections open lazily per
        label on the first frame (labels aren't known until audio
        arrives)."""
        if self._connection_factory is None:
            if not self._sdk_missing_logged:
                logger.error(
                    "SIPREC dispatch: no Deepgram connection factory "
                    "(deepgram-sdk missing or DEEPGRAM_API_KEY unset) — "
                    "SIPREC audio will be dropped for rec=%s tenant=%s",
                    recording_session_id,
                    tenant_id,
                )
                self._sdk_missing_logged = True
            return
        loop = asyncio.get_running_loop()
        async with self._lock:
            if recording_session_id not in self._sessions:
                self._sessions[recording_session_id] = _SessionSinks(
                    live_session_id, loop
                )

    async def send_audio(
        self,
        recording_session_id: str,
        label: str,
        audio_mulaw_8k: bytes,
    ) -> None:
        async with self._lock:
            sink = self._sessions.get(recording_session_id)
        if sink is None:
            # open_session was a no-op (SDK/key missing) — nothing to do.
            return

        conn = sink.connections.get(label)
        if conn is None:
            conn = await self._open_connection(sink, label)
            if conn is None:
                return
        try:
            await conn.send(audio_mulaw_8k)
        except Exception:
            logger.debug(
                "SIPREC dispatch: Deepgram send failed rec=%s label=%s",
                recording_session_id,
                label,
                exc_info=True,
            )

    async def close_session(
        self,
        recording_session_id: str,
        reason: Optional[str] = None,
    ) -> None:
        async with self._lock:
            sink = self._sessions.pop(recording_session_id, None)
        if sink is None:
            return

        for label, conn in list(sink.connections.items()):
            try:
                await conn.finish()
            except Exception:
                logger.debug(
                    "SIPREC dispatch: Deepgram finish failed rec=%s label=%s",
                    recording_session_id,
                    label,
                    exc_info=True,
                )

        # Finalise the buffer into an Interaction + enqueue the pipeline.
        # Keyed by the LiveSession id, which is what _dispatch_batch_analysis
        # looks the session up by.
        try:
            await self._finalizer(self._get_redis(), str(sink.live_session_id))
        except Exception:
            logger.exception(
                "SIPREC dispatch: finalize failed for rec=%s (live_session=%s)",
                recording_session_id,
                sink.live_session_id,
            )

    # ── Connection lifecycle ─────────────────────────────────────────

    async def _open_connection(
        self, sink: _SessionSinks, label: str
    ) -> Optional[Any]:
        async with sink.lock:
            existing = sink.connections.get(label)
            if existing is not None:
                return existing
            if self._connection_factory is None:
                return None
            try:
                conn = self._connection_factory()
                self._attach_transcript_handler(conn, sink, label)
                await conn.start(
                    {
                        "model": self._model,
                        "encoding": "mulaw",
                        "sample_rate": 8000,
                        "channels": 1,
                        "interim_results": False,
                        "punctuate": True,
                    }
                )
            except Exception:
                logger.exception(
                    "SIPREC dispatch: could not start Deepgram connection "
                    "for live_session=%s label=%s",
                    sink.live_session_id,
                    label,
                )
                return None
            sink.connections[label] = conn
            return conn

    def _attach_transcript_handler(
        self, conn: Any, sink: _SessionSinks, label: str
    ) -> None:
        """Register a final-result handler that appends to the Redis buffer.

        The SDK calls this on its own thread; we marshal the async Redis
        append back onto the bridge's event loop. Speaker is the SIP
        ``label`` — one connection per participant stream, so no
        diarization needed.
        """
        try:
            from deepgram import LiveTranscriptionEvents  # type: ignore

            transcript_event: Any = LiveTranscriptionEvents.Transcript
        except Exception:
            # SDK not installed (tests with a fake connection) — register
            # under the wire name so fakes still capture the handler.
            transcript_event = "Results"

        def _on_transcript(_self: Any, result: Any = None, **kwargs: Any) -> None:
            try:
                text, is_final = _extract_final_text(result)
                if not is_final or not text:
                    return
                coro = self._append_segment(sink, label, text)
                try:
                    asyncio.run_coroutine_threadsafe(coro, sink.loop)
                except RuntimeError:
                    # Loop already closed — call tearing down.
                    coro.close()
                    logger.debug("SIPREC transcript dropped: loop closed")
            except Exception:
                logger.debug(
                    "SIPREC dispatch: transcript handler failed", exc_info=True
                )

        try:
            conn.on(transcript_event, _on_transcript)
        except Exception:
            logger.debug(
                "SIPREC dispatch: could not register transcript handler",
                exc_info=True,
            )

    async def _append_segment(
        self, sink: _SessionSinks, label: str, text: str
    ) -> None:
        segment = json.dumps(
            {"text": text, "speaker": label, "timestamp": time.time()}
        )
        try:
            redis = self._get_redis()
            pipe = redis.pipeline(transaction=False)
            pipe.rpush(sink.buffer_key, segment)
            pipe.expire(sink.buffer_key, _LIVE_BUFFER_TTL_SECONDS)
            await pipe.execute()
        except Exception:
            logger.debug(
                "SIPREC dispatch: buffer append failed for %s",
                sink.buffer_key,
                exc_info=True,
            )


def _extract_final_text(result: Any) -> Tuple[str, bool]:
    """Pull ``(transcript_text, is_final)`` from a Deepgram result.

    Handles both the SDK's dataclass-ish objects and plain dicts (the
    shape a test fake or the replay harness produces).
    """
    if result is None:
        return "", False
    is_final = (
        getattr(result, "is_final", None)
        if not isinstance(result, dict)
        else result.get("is_final")
    )
    channel = (
        getattr(result, "channel", None)
        if not isinstance(result, dict)
        else result.get("channel")
    )
    if channel is None:
        return "", bool(is_final)
    alternatives = (
        getattr(channel, "alternatives", None)
        if not isinstance(channel, dict)
        else channel.get("alternatives")
    )
    if not alternatives:
        return "", bool(is_final)
    alt = alternatives[0]
    transcript = (
        getattr(alt, "transcript", None)
        if not isinstance(alt, dict)
        else alt.get("transcript")
    )
    return (transcript or "").strip(), bool(is_final)
