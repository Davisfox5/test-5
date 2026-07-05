"""Bounded media pump — keeps the WebSocket receive loop non-blocking.

Challenge #2b/#2d (docs/complexity/02-realtime-media.md): the Media
Streams handler used to forward each frame to Deepgram *and* run the
Praat snapshot inline in the receive loop, so a 0.5–2 s extraction
stalled audio ingest. The pump decouples them:

* The receive loop calls :meth:`MediaStreamPump.offer` — a synchronous,
  never-blocking enqueue into a bounded queue. When the queue is full
  the **oldest** frame is dropped (live coaching cares about recency),
  the drop is counted in ``linda_live_media_frames_dropped_total``, and
  the first drop per session logs a WARNING.
* A consumer task (:meth:`run`) drains the queue: forwards audio to the
  transcriber, feeds the paralinguistic window, and — at most one at a
  time — kicks a snapshot job onto the executor with a hard deadline.
  A job that overruns the deadline is skipped (nothing published) and
  the window's cadence backs off; a job that completes in budget resets
  the cadence.

Everything that mutates the window happens on the consumer task (the
event loop), preserving the window's single-writer contract. The
executor thread only ever sees the immutable ``SnapshotJob``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# ~5 s of 20 ms Media Streams frames. Overload beyond this is dropped
# oldest-first rather than buffered into unbounded latency.
DEFAULT_QUEUE_MAX_FRAMES = 250

# Praat budget per snapshot. The ~3 s cadence leaves no room for a 2 s
# extraction plus publish; anything over this is skipped as stale.
DEFAULT_SNAPSHOT_DEADLINE_SEC = 2.5

_SENTINEL = None


class MediaStreamPump:
    """One per live call. See module docstring.

    ``send_audio`` — async callable forwarding a frame to the
    transcriber (Deepgram). Errors are logged and do not stop the pump.
    ``publish`` — async callable receiving a completed
    ``ParalinguisticFeatures``; only invoked for in-budget snapshots.
    ``window`` — a ``LiveParalinguisticWindow`` or None when the tenant
    doesn't have the live paralinguistic feature enabled.
    """

    def __init__(
        self,
        *,
        send_audio: Callable[[bytes], Awaitable[None]],
        window: Optional[Any] = None,
        publish: Optional[Callable[[Any], Awaitable[None]]] = None,
        provider: str = "twilio",
        queue_max_frames: int = DEFAULT_QUEUE_MAX_FRAMES,
        snapshot_deadline_sec: float = DEFAULT_SNAPSHOT_DEADLINE_SEC,
        initial_audio_seconds: float = 0.0,
        bytes_per_second: int = 8000,  # μ-law: 1 byte/sample at 8 kHz
    ) -> None:
        self._send_audio = send_audio
        self._window = window
        self._publish = publish
        self._provider = provider
        self._deadline = snapshot_deadline_sec
        self._queue: "asyncio.Queue" = asyncio.Queue(maxsize=queue_max_frames)
        self._snapshot_task: Optional["asyncio.Task"] = None
        self._closed = False
        self._bytes_per_second = bytes_per_second
        self.frames_dropped = 0
        self.frames_offered = 0
        # Cumulative audio position for the whole call, carried across
        # reconnects — the grace-period resume path persists this on
        # disconnect and seeds it on the next attach.
        self.audio_seconds = initial_audio_seconds

    # ── Producer side (receive loop) ────────────────────────────────

    def offer(self, audio: bytes) -> bool:
        """Enqueue a frame without ever blocking.

        Returns False when the queue was full and the oldest frame was
        dropped to make room (the new frame is always kept).
        """
        if self._closed or not audio:
            return True
        self.frames_offered += 1
        # Position advances for every frame the provider sent us, even
        # ones the bounded queue later sheds — the stream's timeline
        # moved regardless.
        self.audio_seconds += len(audio) / float(self._bytes_per_second)
        try:
            self._queue.put_nowait(audio)
            return True
        except asyncio.QueueFull:
            pass
        try:
            self._queue.get_nowait()  # drop oldest — recency wins
        except asyncio.QueueEmpty:  # pragma: no cover — race with consumer
            pass
        try:
            self._queue.put_nowait(audio)
        except asyncio.QueueFull:  # pragma: no cover — single producer
            pass
        self.frames_dropped += 1
        if self.frames_dropped == 1:
            logger.warning(
                "Live media queue full (provider=%s) — dropping oldest frames; "
                "further drops counted in metrics only",
                self._provider,
            )
        try:
            from backend.app.services.metrics import LIVE_MEDIA_FRAMES_DROPPED

            LIVE_MEDIA_FRAMES_DROPPED.labels(provider=self._provider).inc()
        except Exception:
            pass
        return False

    # ── Consumer side ───────────────────────────────────────────────

    async def run(self) -> None:
        """Drain the queue until :meth:`aclose` is called."""
        while True:
            audio = await self._queue.get()
            if audio is _SENTINEL:
                break
            try:
                await self._send_audio(audio)
            except Exception:
                logger.debug("media pump: transcriber send failed", exc_info=True)
            if self._window is not None:
                self._window.feed(audio)
                self._maybe_start_snapshot()
        if self._snapshot_task is not None and not self._snapshot_task.done():
            self._snapshot_task.cancel()

    def _maybe_start_snapshot(self) -> None:
        # In-flight guard: at most one snapshot at a time. The window's
        # own cadence limiter handles the "how often" question.
        if self._snapshot_task is not None and not self._snapshot_task.done():
            return
        job = self._window.maybe_begin_snapshot()
        if job is None:
            return
        self._snapshot_task = asyncio.get_event_loop().create_task(
            self._run_snapshot(job)
        )

    async def _run_snapshot(self, job: Any) -> None:
        try:
            from backend.app.services.metrics import LIVE_PARALINGUISTIC_SNAPSHOTS
        except Exception:
            LIVE_PARALINGUISTIC_SNAPSHOTS = None  # type: ignore

        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(None, job.run)
        try:
            features = await asyncio.wait_for(future, timeout=self._deadline)
        except asyncio.TimeoutError:
            # The Praat thread keeps running (uncancellable C code) but
            # its result is stale — skip it and widen the cadence so we
            # don't queue up behind a slow extractor.
            self._window.note_overrun()
            logger.info(
                "paralinguistic snapshot overran %.1fs budget — skipped "
                "(provider=%s)",
                self._deadline,
                self._provider,
            )
            if LIVE_PARALINGUISTIC_SNAPSHOTS is not None:
                LIVE_PARALINGUISTIC_SNAPSHOTS.labels(status="overrun").inc()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("paralinguistic snapshot failed", exc_info=True)
            if LIVE_PARALINGUISTIC_SNAPSHOTS is not None:
                LIVE_PARALINGUISTIC_SNAPSHOTS.labels(status="error").inc()
            return

        self._window.note_ok()
        if features is None:
            if LIVE_PARALINGUISTIC_SNAPSHOTS is not None:
                LIVE_PARALINGUISTIC_SNAPSHOTS.labels(status="short_buffer").inc()
            return
        if not getattr(features, "available", False):
            return
        if LIVE_PARALINGUISTIC_SNAPSHOTS is not None:
            LIVE_PARALINGUISTIC_SNAPSHOTS.labels(status="emitted").inc()
        if self._publish is not None:
            try:
                await self._publish(features)
            except Exception:
                logger.debug("paralinguistic publish failed", exc_info=True)

    # ── Shutdown ────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Signal the consumer to finish draining and stop.

        Safe to call more than once. Frames offered after close are
        ignored.
        """
        if self._closed:
            return
        self._closed = True
        # The sentinel must land even when the queue is full.
        while True:
            try:
                self._queue.put_nowait(_SENTINEL)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass


__all__ = ["MediaStreamPump", "DEFAULT_QUEUE_MAX_FRAMES", "DEFAULT_SNAPSHOT_DEADLINE_SEC"]
