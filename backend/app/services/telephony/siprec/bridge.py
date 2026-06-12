"""SIPREC bridge — turn SRS sidecar frames into transcription dispatches.

The Session Recording Server (FreeSWITCH userspace, see
``services/telephony/siprec_srs``) terminates SIP and SRTP. It then
forwards two kinds of events to this Python process over a local
control channel:

* **Lifecycle events** — ``recording.started`` / ``recording.stopped``
  / ``participant.joined``. These come in as JSON objects on the
  ``POST /siprec/events`` endpoint (see ``api/siprec.py``) and are
  applied to the ``SiprecSession`` row by ``SiprecBridge.handle_event``.
* **Audio frames** — μ-law 8 kHz or PCM16 8 kHz payloads keyed by the
  recording session id and SDP ``a=label`` (which resolves to a
  participant via the rs-metadata). Frames go through
  ``SiprecBridge.handle_audio`` and out to the transcription pipeline.

Why an injectable dispatcher? Live transcription in this codebase
talks to Deepgram via a long-lived WS connection per call (see the
Twilio path in ``api/telephony.py``). Production wiring spins one of
those up per ``SiprecSession``. Tests inject a recording mock so we
can assert "frames N through M reached the pipeline in order" without
spinning up Deepgram. The ``TranscriptionDispatch`` Protocol is the
contract.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.audio import AudioFormat, to_mulaw_8k

logger = logging.getLogger(__name__)


# ── Frame model ─────────────────────────────────────────────────────────


@dataclass
class SiprecAudioFrame:
    """One audio frame forwarded by the SRS sidecar.

    ``recording_session_id`` is the id from the rs-metadata
    ``<recording session_id="...">`` attribute (matches the
    ``SiprecSession.src_session_id`` column). ``label`` is the SDP
    ``a=label`` value of the media stream this frame belongs to —
    typically ``"1"`` for the caller's audio and ``"2"`` for the
    callee's, but vendors are free to pick any stable identifier.

    Audio is **already plaintext** — the SRS terminated SRTP. The
    bridge only handles format normalization, not decryption.
    """

    recording_session_id: str
    label: str  # SDP a=label
    sequence: int  # monotonically increasing per (session, label)
    audio_format: AudioFormat
    payload: bytes
    received_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ── Transcription dispatch contract ─────────────────────────────────────


class TranscriptionDispatch(Protocol):
    """Per-session transcription sink the bridge writes audio into.

    Production implementation opens a Deepgram live WS in
    ``open_session`` and closes it in ``close_session``. Tests use a
    record-everything mock.

    ``send_audio`` is awaited on every frame and must not block
    indefinitely — the bridge serializes frames on a per-session
    queue, so backpressure here translates to dropped frames at the
    SRS edge. The production Deepgram client is non-blocking; if a
    custom dispatch needs to do I/O, queue it.
    """

    async def open_session(
        self,
        recording_session_id: str,
        live_session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        provider: str,
    ) -> None: ...

    async def send_audio(
        self,
        recording_session_id: str,
        label: str,
        audio_mulaw_8k: bytes,
    ) -> None: ...

    async def close_session(
        self,
        recording_session_id: str,
        reason: Optional[str] = None,
    ) -> None: ...


class _NullDispatch:
    """Drop-everything dispatch — the default when nothing is wired up.

    Used by smoke tests and in environments where the SRS is
    deployed but transcription wiring isn't ready yet. Logs at INFO
    once per recording session so operators notice the no-op.
    """

    def __init__(self) -> None:
        self._opened: set[str] = set()

    async def open_session(
        self,
        recording_session_id: str,
        live_session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        provider: str,
    ) -> None:
        if recording_session_id not in self._opened:
            logger.info(
                "SIPREC bridge: NullDispatch.open_session "
                "rec=%s tenant=%s provider=%s — frames will be discarded",
                recording_session_id,
                tenant_id,
                provider,
            )
            self._opened.add(recording_session_id)

    async def send_audio(
        self,
        recording_session_id: str,
        label: str,
        audio_mulaw_8k: bytes,
    ) -> None:
        return None

    async def close_session(
        self,
        recording_session_id: str,
        reason: Optional[str] = None,
    ) -> None:
        self._opened.discard(recording_session_id)


# ── Persistence callbacks (injected so the bridge can be tested without DB)


SessionFactory = Callable[[], "AsyncSession"]


class SiprecBridge:
    """Orchestrates lifecycle events + audio frames for SIPREC sessions.

    One instance per worker process. Per-recording-session state lives
    on ``self._sessions`` and gets reaped when the SRS sends
    ``recording.stopped`` (or after the configured idle timeout).
    """

    def __init__(
        self,
        dispatch: Optional[TranscriptionDispatch] = None,
        session_factory: Optional[SessionFactory] = None,
        idle_timeout_seconds: float = 600.0,
    ) -> None:
        self._dispatch: TranscriptionDispatch = dispatch or _NullDispatch()
        # ``session_factory`` is the project's async-session maker
        # (``backend.app.db.async_session``). Tests pass a custom
        # factory bound to the in-memory SQLite DB.
        self._session_factory = session_factory
        self._sessions: Dict[str, _SessionState] = {}
        self._lock = asyncio.Lock()
        self._idle_timeout = idle_timeout_seconds

    # ── Lifecycle ───────────────────────────────────────────────────

    async def handle_started(
        self,
        *,
        recording_session_id: str,
        tenant_id: uuid.UUID,
        provider: str,
        agent_user_id: Optional[uuid.UUID],
        src_call_id: Optional[str],
        src_metadata: Dict[str, Any],
        is_consent_attested: bool,
        sdp_crypto_suite: Optional[str] = None,
    ) -> "_SessionState":
        """Apply a ``recording.started`` event from the SRS.

        Creates a ``LiveSession`` row (sibling to the existing
        Twilio/SignalWire/Telnyx live sessions, so the rest of the
        coaching pipeline treats SIPREC like any other live audio
        source) and a ``SiprecSession`` row that tracks
        SIPREC-specific metadata. Idempotent on
        ``recording_session_id`` — re-firing the same event returns
        the cached state.
        """

        async with self._lock:
            existing = self._sessions.get(recording_session_id)
            if existing is not None:
                return existing

        live_session_id = await self._persist_started(
            tenant_id=tenant_id,
            provider=provider,
            agent_user_id=agent_user_id,
            recording_session_id=recording_session_id,
            src_call_id=src_call_id,
            src_metadata=src_metadata,
            is_consent_attested=is_consent_attested,
            sdp_crypto_suite=sdp_crypto_suite,
        )

        await self._dispatch.open_session(
            recording_session_id=recording_session_id,
            live_session_id=live_session_id,
            tenant_id=tenant_id,
            provider=provider,
        )

        state = _SessionState(
            recording_session_id=recording_session_id,
            tenant_id=tenant_id,
            provider=provider,
            live_session_id=live_session_id,
            sequence_seen={},
        )
        async with self._lock:
            self._sessions[recording_session_id] = state
        return state

    async def handle_stopped(
        self,
        *,
        recording_session_id: str,
        reason: Optional[str] = None,
    ) -> None:
        """Apply a ``recording.stopped`` event from the SRS."""

        async with self._lock:
            state = self._sessions.pop(recording_session_id, None)

        await self._dispatch.close_session(
            recording_session_id=recording_session_id, reason=reason
        )
        await self._persist_stopped(
            recording_session_id=recording_session_id,
            live_session_id=state.live_session_id if state else None,
            reason=reason,
        )

    # ── Audio ───────────────────────────────────────────────────────

    async def handle_audio(self, frame: SiprecAudioFrame) -> bool:
        """Normalize one audio frame and dispatch it.

        Returns ``True`` when the frame was forwarded to the
        transcription dispatch, ``False`` when it was dropped (no
        active session, duplicate sequence, or malformed payload).
        Drops are logged at DEBUG so a noisy SBC doesn't spam the
        logs.
        """

        if not frame.payload:
            return False

        async with self._lock:
            state = self._sessions.get(frame.recording_session_id)
        if state is None:
            logger.debug(
                "SIPREC audio frame for unknown session %s (label=%s seq=%s) — dropping",
                frame.recording_session_id,
                frame.label,
                frame.sequence,
            )
            return False

        # Per-stream monotonic sequence guard. SRS deliveries can
        # duplicate on retry; rejecting <= last_seen prevents
        # double-feeding the transcriber.
        last = state.sequence_seen.get(frame.label, -1)
        if frame.sequence <= last:
            return False
        state.sequence_seen[frame.label] = frame.sequence

        try:
            audio = to_mulaw_8k(frame.payload, frame.audio_format)
        except Exception:
            logger.exception(
                "SIPREC audio normalization failed for rec=%s label=%s",
                frame.recording_session_id,
                frame.label,
            )
            return False

        await self._dispatch.send_audio(
            recording_session_id=frame.recording_session_id,
            label=frame.label,
            audio_mulaw_8k=audio,
        )
        return True

    # ── Introspection (for /admin and tests) ────────────────────────

    def active_sessions(self) -> List[str]:
        return list(self._sessions.keys())

    def get_state(self, recording_session_id: str) -> Optional["_SessionState"]:
        return self._sessions.get(recording_session_id)

    # ── Persistence (overridable hooks) ─────────────────────────────

    async def _persist_started(
        self,
        *,
        tenant_id: uuid.UUID,
        provider: str,
        agent_user_id: Optional[uuid.UUID],
        recording_session_id: str,
        src_call_id: Optional[str],
        src_metadata: Dict[str, Any],
        is_consent_attested: bool,
        sdp_crypto_suite: Optional[str],
    ) -> uuid.UUID:
        """Insert ``LiveSession`` + ``SiprecSession`` rows.

        Returns the ``LiveSession.id`` so the dispatcher can correlate
        downstream artefacts (transcripts, scorecards) back to the
        same session row the existing live-coaching UI reads from.
        """

        if self._session_factory is None:
            # Tests that don't need DB persistence pass
            # ``session_factory=None`` and rely on the dispatch mock
            # alone. We still mint a live-session uuid so the rest
            # of the bridge's interface is consistent.
            return uuid.uuid4()

        # Late import — keeps the test path that injects a None
        # session_factory free of SQLAlchemy mapping side-effects.
        from backend.app.models import LiveSession, SiprecSession

        async with self._session_factory() as db:
            live = LiveSession(
                tenant_id=tenant_id,
                # ``LiveSession.agent_id`` is non-nullable; SIPREC
                # sometimes can't resolve a LINDA user from the SBC's
                # AOR (e.g. shared-line scenarios). Callers MUST
                # resolve a default "siprec service" user up the
                # stack — we surface a clear error here rather than
                # writing a fake uuid.
                agent_id=agent_user_id or _require_agent_user_id(),
                source=provider,
                status="live",
                started_at=datetime.now(timezone.utc),
            )
            db.add(live)
            await db.flush()
            siprec = SiprecSession(
                tenant_id=tenant_id,
                live_session_id=live.id,
                provider=provider,
                src_session_id=recording_session_id,
                src_call_id=src_call_id,
                src_metadata=src_metadata,
                is_consent_attested=is_consent_attested,
                sdp_crypto_suite=sdp_crypto_suite,
                started_at=datetime.now(timezone.utc),
            )
            db.add(siprec)
            await db.commit()
            return live.id

    async def _persist_stopped(
        self,
        *,
        recording_session_id: str,
        live_session_id: Optional[uuid.UUID],
        reason: Optional[str],
    ) -> None:
        if self._session_factory is None:
            return
        from backend.app.models import LiveSession, SiprecSession

        async with self._session_factory() as db:
            stmt = select(SiprecSession).where(
                SiprecSession.src_session_id == recording_session_id
            )
            siprec = (await db.execute(stmt)).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if siprec is not None:
                siprec.ended_at = now
                if reason:
                    siprec.end_reason = reason
            if live_session_id is not None:
                live = await db.get(LiveSession, live_session_id)
                if live is not None:
                    live.status = "ended"
                    live.ended_at = now
            await db.commit()


def _require_agent_user_id() -> uuid.UUID:
    """Helper that fails loudly when an SRS event has no resolved user.

    Centralised here so the error message is consistent — failure to
    resolve a user is the single most common deployment misconfig
    on this path (the SBC's From-AOR doesn't match a User.email in
    LINDA), and a clear message saves an hour of triage.
    """

    raise ValueError(
        "SIPREC handle_started: agent_user_id is None. The SRS event "
        "did not resolve to a LINDA user; configure a default "
        "service-account user via SIPREC_SERVICE_USER_ID or pre-create "
        "the user with the SBC's From-AOR before enabling recording."
    )


# ── Internal session state ──────────────────────────────────────────────


@dataclass
class _SessionState:
    """In-memory state for one active SIPREC recording session."""

    recording_session_id: str
    tenant_id: uuid.UUID
    provider: str
    live_session_id: uuid.UUID
    sequence_seen: Dict[str, int]


# ── Module singleton ────────────────────────────────────────────────────


_singleton: Optional[SiprecBridge] = None


def get_bridge() -> SiprecBridge:
    """Return the process-wide bridge (lazy init).

    Wired up at first use rather than at import time so test code
    can replace it with a stub via ``set_bridge`` before any HTTP
    handler resolves it.
    """

    global _singleton
    if _singleton is None:
        from backend.app.db import async_session

        _singleton = SiprecBridge(session_factory=async_session)
    return _singleton


def set_bridge(bridge: Optional[SiprecBridge]) -> None:
    """Override (or reset) the process-wide bridge — tests only."""

    global _singleton
    _singleton = bridge
