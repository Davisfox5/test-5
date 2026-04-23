"""Live paralinguistic extraction — rolling μ-law audio window.

Sibling to ``live_coaching_features.LiveFeatureWindow`` (text-only). A
``LiveParalinguisticWindow`` instance is owned by the Media Streams
WebSocket handler for the duration of a call. Raw μ-law bytes arrive
per frame, get decoded to PCM16, and land in a circular buffer of the
last ``window_sec`` seconds of audio.

Every ``recompute_every_sec`` the buffer is dumped to a NamedTempFile
and handed to the existing :class:`ParalinguisticExtractor` for a
snapshot. The expensive Praat work runs in a thread-pool executor so
it never blocks the WS read loop.

**Speaker assignment deliberately stays out of the live path.** The
provider-reported ``track`` (inbound/outbound) doesn't reliably map to
agent-vs-customer across warm-transferred lines, conferenced calls, or
shared-seat setups, so we compute acoustic features on the **whole
window** and let the scanner act on those aggregates. When we're ready
for per-speaker live features we'll pull the timeline from Deepgram's
live diarization event stream and slice the buffer by those ranges —
provider-agnostic and grounded in actual speaker IDs, not leg metadata.
"""

from __future__ import annotations

import audioop
import logging
import os
import tempfile
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from backend.app.services.paralinguistics import (
    ParalinguisticFeatures,
    SpeakerAudioSegment,
    get_paralinguistic_extractor,
)

logger = logging.getLogger(__name__)


@dataclass
class _Chunk:
    """One decoded audio chunk from Media Streams. We no longer carry
    a speaker label — see the module docstring for the rationale."""

    payload: bytes  # μ-law, 8 kHz, 1 channel
    timestamp: float


class LiveParalinguisticWindow:
    """Rolling audio window + scheduled recompute.

    Call order:
    ``feed(payload)`` on every media frame → cheap.
    ``maybe_snapshot()`` whenever you're willing to pay the compute
    cost → returns a :class:`ParalinguisticFeatures` snapshot when the
    recompute interval has elapsed, or None otherwise.
    """

    # Synthetic id used to label the whole-window snapshot. The
    # downstream scanner looks this up when per-speaker data isn't
    # available and applies its thresholds to the aggregate.
    WHOLE_WINDOW_SPEAKER_ID = "window"

    def __init__(
        self,
        *,
        sample_rate: int = 8000,
        window_sec: float = 30.0,
        recompute_every_sec: float = 3.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.window_sec = window_sec
        self.recompute_every_sec = recompute_every_sec
        self._chunks: Deque[_Chunk] = deque()
        self._call_start = time.time()
        self._last_snapshot_at: float = 0.0
        self._extractor = get_paralinguistic_extractor()

    # ── Frame intake ─────────────────────────────────────────────────

    def feed(self, payload: bytes) -> None:
        if not payload:
            return
        now = time.time()
        self._chunks.append(_Chunk(payload=payload, timestamp=now))
        cutoff = now - self.window_sec
        while self._chunks and self._chunks[0].timestamp < cutoff:
            self._chunks.popleft()

    # ── Snapshot ─────────────────────────────────────────────────────

    def maybe_snapshot(self) -> Optional[ParalinguisticFeatures]:
        """Return a snapshot if ``recompute_every_sec`` has elapsed."""
        now = time.time()
        if now - self._last_snapshot_at < self.recompute_every_sec:
            return None
        self._last_snapshot_at = now
        return self._snapshot_now()

    def _snapshot_now(self) -> Optional[ParalinguisticFeatures]:
        if not self._chunks:
            return None
        # Decode the entire window to PCM16 in one pass — audioop carries
        # no state across calls for ulaw2lin, so concatenation is safe.
        concat = b"".join(c.payload for c in self._chunks)
        try:
            pcm = audioop.ulaw2lin(concat, 2)
        except Exception:
            logger.exception("ulaw decode failed in live paralinguistic window")
            return None

        # Write a one-shot WAV so parselmouth can read it. Tempfile is
        # owned by this method and unlinked in the finally block — we
        # never keep audio on disk across calls.
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="linda-live-para-",
                suffix=".wav",
                delete=False,
            ) as tmp:
                tmp_path = tmp.name
            with wave.open(tmp_path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
                wav.writeframes(pcm)

            window_start = self._chunks[0].timestamp
            window_end = self._chunks[-1].timestamp
            duration = max(0.0, window_end - window_start)
            if duration < 1.5:
                return None

            # Whole-window segment — the extractor will still produce
            # per-speaker structure (a single synthetic speaker in the
            # live case) so scorers and scanners can read the same
            # shape post-call and live.
            segments = [
                SpeakerAudioSegment(
                    speaker_id=self.WHOLE_WINDOW_SPEAKER_ID,
                    start=0.0,
                    end=duration,
                )
            ]
            return self._extractor.extract(segments, audio_path=tmp_path)
        except Exception:
            logger.exception("Live paralinguistic snapshot failed")
            return None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    logger.debug("live para tempfile unlink failed", exc_info=True)


__all__ = ["LiveParalinguisticWindow"]
