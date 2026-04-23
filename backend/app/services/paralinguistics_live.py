"""Live paralinguistic extraction — rolling μ-law audio window with
per-speaker slicing driven by Deepgram's live diarization stream.

Sibling to ``live_coaching_features.LiveFeatureWindow`` (text-only). A
``LiveParalinguisticWindow`` instance is owned by the Media Streams
WebSocket handler for the duration of a call.

Two inputs:

1. ``feed(payload)`` — μ-law frames as they arrive from the provider
   (Twilio/SignalWire/Telnyx). The window keeps the last
   ``window_sec`` seconds of audio in a circular buffer, timestamped
   at arrival.
2. ``update_diarization(turns)`` — ``(start_offset, end_offset,
   speaker_id)`` triples pulled from Deepgram's live ``Results``
   events. Offsets are seconds since the call started.

At snapshot time the window slices its audio by the diarization
timeline and produces per-speaker acoustic features. When no
timeline has arrived yet (the first ~1-2 seconds of a call, or if
diarization is disabled) we fall back to whole-window features under
a synthetic ``window`` speaker id — the same shape the scanner is
already designed around.

Deliberately agnostic of provider leg metadata (Twilio's ``track``,
Telnyx's ``track_type``): those don't reliably map to agent vs.
customer across warm transfers, conferences, or shared-seat setups.
Diarization is the right abstraction.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

from backend.app.services.audio_codecs import ulaw_to_pcm16
from backend.app.services.paralinguistics import (
    ParalinguisticFeatures,
    SpeakerAudioSegment,
    get_paralinguistic_extractor,
)

logger = logging.getLogger(__name__)


@dataclass
class _Chunk:
    """One decoded audio chunk from Media Streams. ``offset`` is seconds
    since the call started (i.e., since ``feed`` was first called),
    matching Deepgram's event timing so slicing is simple."""

    payload: bytes  # μ-law, 8 kHz, 1 channel
    offset: float   # seconds since call start


@dataclass
class DiarTurn:
    """One diarized speaker turn, in call-relative time.

    ``start`` and ``end`` are offsets in seconds from the call start.
    ``speaker`` is whatever label Deepgram assigned (stringified
    integer 0, 1, … for cloud diarization).
    """

    start: float
    end: float
    speaker: str


class LiveParalinguisticWindow:
    """Rolling audio window + scheduled per-speaker recompute.

    Call order:

    * ``feed(payload)`` on every media frame → cheap, no Praat.
    * ``update_diarization(turns)`` whenever Deepgram emits a
      ``Results`` event carrying diarized words. Overlapping turns
      replace older ones for the same time range; the internal
      timeline stays monotonic.
    * ``maybe_snapshot()`` whenever you're willing to pay the compute
      cost → returns a :class:`ParalinguisticFeatures` snapshot when
      the recompute interval has elapsed, or None otherwise.
    """

    # Synthetic id used when no diarization timeline has arrived yet.
    # The scanner treats it identically to a real per-speaker row.
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
        # Wall-clock anchor. We stamp chunks with call-relative offsets
        # (not wall time) so the buffer and Deepgram's event offsets
        # share a coordinate system.
        self._call_start = time.time()
        self._last_snapshot_at: float = 0.0
        self._extractor = get_paralinguistic_extractor()
        # Flat list of diarization turns, sorted by start. Kept in
        # call-relative time; trimmed when we prune the audio buffer.
        self._diar_turns: List[DiarTurn] = []

    # ── Frame intake ─────────────────────────────────────────────────

    def feed(self, payload: bytes) -> None:
        if not payload:
            return
        offset = time.time() - self._call_start
        self._chunks.append(_Chunk(payload=payload, offset=offset))
        cutoff = offset - self.window_sec
        while self._chunks and self._chunks[0].offset < cutoff:
            self._chunks.popleft()
        # Trim diarization turns that ended before the audio we still
        # hold. Saves us from scanning stale turns on every snapshot.
        while self._diar_turns and self._diar_turns[0].end < cutoff:
            self._diar_turns.pop(0)

    # ── Diarization intake ──────────────────────────────────────────

    def update_diarization(self, turns: List[DiarTurn]) -> None:
        """Merge a batch of diarization turns into the timeline.

        Turns are expected in call-relative seconds. The caller doesn't
        need to dedupe — we collapse adjacent same-speaker turns and
        drop exact duplicates. The timeline stays sorted by ``start``.
        """
        if not turns:
            return
        for t in turns:
            if t.end <= t.start:
                continue
            self._diar_turns.append(t)
        self._diar_turns.sort(key=lambda x: x.start)
        self._diar_turns = _collapse_adjacent_turns(self._diar_turns)

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
        # Decode the entire window to PCM16 in one pass. The μ-law
        # decoder is stateless, so concatenating per-chunk bytes and
        # decoding once is equivalent to decoding each chunk separately.
        concat = b"".join(c.payload for c in self._chunks)
        try:
            pcm = ulaw_to_pcm16(concat)
        except Exception:
            logger.exception("ulaw decode failed in live paralinguistic window")
            return None

        window_start_offset = self._chunks[0].offset
        window_end_offset = self._chunks[-1].offset
        duration = max(0.0, window_end_offset - window_start_offset)
        if duration < 1.5:
            return None

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

            segments = self._build_segments(
                window_start_offset=window_start_offset,
                window_end_offset=window_end_offset,
            )
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

    def _build_segments(
        self,
        *,
        window_start_offset: float,
        window_end_offset: float,
    ) -> List[SpeakerAudioSegment]:
        """Turn the diarization timeline into :class:`SpeakerAudioSegment`
        entries whose times are relative to the start of the WAV buffer
        (not the call). When there's no diarization data, return one
        whole-window segment under the synthetic speaker id so the
        scanner's fallback path still fires.

        A single speaker that talked across multiple turns ends up with
        multiple segments in the output — the extractor merges them
        when it averages per-speaker features, so the aggregation is
        still a single row per speaker in the returned
        ``ParalinguisticFeatures``.
        """
        if not self._diar_turns:
            return [
                SpeakerAudioSegment(
                    speaker_id=self.WHOLE_WINDOW_SPEAKER_ID,
                    start=0.0,
                    end=window_end_offset - window_start_offset,
                )
            ]

        segments: List[SpeakerAudioSegment] = []
        for turn in self._diar_turns:
            # Clamp to the audio we actually have on hand.
            start = max(turn.start, window_start_offset)
            end = min(turn.end, window_end_offset)
            if end <= start:
                continue
            segments.append(
                SpeakerAudioSegment(
                    speaker_id=turn.speaker,
                    start=start - window_start_offset,
                    end=end - window_start_offset,
                )
            )
        if not segments:
            # Timeline exists but doesn't overlap the current audio —
            # fall back so we still return something useful.
            return [
                SpeakerAudioSegment(
                    speaker_id=self.WHOLE_WINDOW_SPEAKER_ID,
                    start=0.0,
                    end=window_end_offset - window_start_offset,
                )
            ]
        return segments


def _collapse_adjacent_turns(turns: List[DiarTurn]) -> List[DiarTurn]:
    """Merge adjacent turns that belong to the same speaker and drop
    exact duplicates. Input must already be sorted by ``start``."""
    if len(turns) <= 1:
        return turns
    out: List[DiarTurn] = [turns[0]]
    for t in turns[1:]:
        prev = out[-1]
        if t.start == prev.start and t.end == prev.end and t.speaker == prev.speaker:
            continue  # duplicate
        if t.speaker == prev.speaker and t.start <= prev.end + 0.1:
            prev.end = max(prev.end, t.end)
            continue
        out.append(t)
    return out


def diar_turns_from_deepgram_words(
    words: List[dict],
    call_start_offset: float = 0.0,
) -> List[DiarTurn]:
    """Convert Deepgram's per-word list (from a ``Results`` event) into
    ``DiarTurn`` entries by collapsing consecutive words with the same
    ``speaker`` label. Safe to call with partial results — the output
    grows monotonically as more words arrive.

    ``call_start_offset`` adjusts Deepgram's word timings if the caller
    needs to shift the timeline (e.g. a reconnect mid-call). Default
    zero — Deepgram's offsets are already relative to the start of the
    websocket connection, which we set to the call start.
    """
    if not words:
        return []
    turns: List[DiarTurn] = []
    current: Optional[DiarTurn] = None
    for w in words:
        speaker = w.get("speaker")
        start = w.get("start")
        end = w.get("end")
        if speaker is None or start is None or end is None:
            continue
        speaker_id = str(speaker)
        start_f = float(start) + call_start_offset
        end_f = float(end) + call_start_offset
        if current is None or current.speaker != speaker_id:
            if current is not None:
                turns.append(current)
            current = DiarTurn(start=start_f, end=end_f, speaker=speaker_id)
        else:
            current.end = max(current.end, end_f)
    if current is not None:
        turns.append(current)
    return turns


__all__ = [
    "DiarTurn",
    "LiveParalinguisticWindow",
    "diar_turns_from_deepgram_words",
]
