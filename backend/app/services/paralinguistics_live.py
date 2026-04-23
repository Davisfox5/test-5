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

Outputs the same ``ParalinguisticFeatures`` shape the post-call
pipeline uses, so downstream consumers (Redis publisher, scanner,
scoring factors) don't need a second code path.
"""

from __future__ import annotations

import audioop
import io
import logging
import os
import tempfile
import time
import wave
from dataclasses import dataclass
from typing import Deque, Optional
from collections import deque

from backend.app.services.paralinguistics import (
    ParalinguisticFeatures,
    SpeakerAudioSegment,
    get_paralinguistic_extractor,
)

logger = logging.getLogger(__name__)


@dataclass
class _Chunk:
    """A μ-law frame from Twilio/SignalWire/Telnyx Media Streams.

    Two tracks — ``inbound`` is whoever called us (customer on an
    inbound call, agent on an outbound), ``outbound`` is the other
    leg. We map these to stable speaker ids so post-hoc joins with
    diarized text land on the right person.
    """

    payload: bytes  # μ-law, 8 kHz, 1 channel
    timestamp: float
    speaker_id: str


class LiveParalinguisticWindow:
    """Rolling audio window + scheduled recompute.

    Call order:
    ``feed(payload, speaker_id)`` on every media frame → cheap.
    ``maybe_snapshot()`` whenever you're willing to pay the compute
    cost → returns a :class:`ParalinguisticFeatures` snapshot when the
    recompute interval has elapsed, or None otherwise.
    """

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

    def feed(self, payload: bytes, *, speaker_id: str = "agent") -> None:
        if not payload:
            return
        now = time.time()
        self._chunks.append(_Chunk(payload=payload, timestamp=now, speaker_id=speaker_id))
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

            # Collapse the per-chunk speaker labels into one segment per
            # speaker covering the whole window. parselmouth will slice
            # the audio itself using these times, so overlapping labels
            # just mean we're computing per-speaker acoustic features on
            # the whole window (cheap and fine).
            window_start = self._chunks[0].timestamp
            window_end = self._chunks[-1].timestamp
            duration = max(0.0, window_end - window_start)
            if duration < 1.5:
                return None

            speakers = {c.speaker_id for c in self._chunks}
            segments = [
                SpeakerAudioSegment(
                    speaker_id=spk,
                    start=0.0,
                    end=duration,
                )
                for spk in sorted(speakers)
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
