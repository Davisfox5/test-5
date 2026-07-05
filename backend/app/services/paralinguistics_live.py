"""Live paralinguistic extraction — rolling audio window with
per-speaker slicing driven by Deepgram's live diarization stream.

Sibling to ``live_coaching_features.LiveFeatureWindow`` (text-only). A
``LiveParalinguisticWindow`` instance is owned by the Media Streams
WebSocket handler for the duration of a call.

Two inputs:

1. ``feed(payload)`` — μ-law frames as they arrive from the provider
   (Twilio/SignalWire/Telnyx). The window keeps the last
   ``window_sec`` seconds of audio in a circular buffer, timestamped
   at arrival. Frames are decoded to PCM16 on the way in (the μ-law
   codec is stateless and cheap), so snapshot time never re-decodes
   the whole window.
2. ``update_diarization(turns)`` — ``(start_offset, end_offset,
   speaker_id)`` triples pulled from Deepgram's live ``Results``
   events. Offsets are seconds since the call started.

Threading contract — **single-writer**:

Every method on the window must be called from ONE thread (in
production: the event loop). The Deepgram SDK fires its handlers on a
private thread; ``api/telephony.py`` marshals those onto the loop with
``call_soon_threadsafe`` instead of touching the window directly. The
expensive Praat work never sees the window at all: the loop thread
calls :meth:`LiveParalinguisticWindow.maybe_begin_snapshot`, which
copies everything the computation needs into an immutable
:class:`SnapshotJob`, and only the job travels to the executor thread.
This makes cross-thread races impossible by construction — there is
nothing shared to race on.

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
from typing import Any, Deque, List, Optional

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

    pcm: bytes      # PCM16 little-endian, 8 kHz, 1 channel
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


class SnapshotJob:
    """Immutable inputs for one paralinguistic snapshot.

    Built on the writer thread by
    :meth:`LiveParalinguisticWindow.maybe_begin_snapshot`; ``run()`` is
    pure with respect to the window and safe to execute on any thread
    (in production: the default thread-pool executor, so Praat never
    blocks the event loop).
    """

    def __init__(
        self,
        *,
        pcm: bytes,
        sample_rate: int,
        window_start_offset: float,
        window_end_offset: float,
        turns: List[DiarTurn],
        extractor: Any,
    ) -> None:
        self.pcm = pcm
        self.sample_rate = sample_rate
        self.window_start_offset = window_start_offset
        self.window_end_offset = window_end_offset
        self.turns = turns
        self._extractor = extractor

    def run(self) -> Optional[ParalinguisticFeatures]:
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
                wav.writeframes(self.pcm)

            segments = _build_segments_from_turns(
                self.turns,
                window_start_offset=self.window_start_offset,
                window_end_offset=self.window_end_offset,
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


class LiveParalinguisticWindow:
    """Rolling audio window + scheduled per-speaker recompute.

    Call order (all on the writer thread — see the module docstring):

    * ``feed(payload)`` on every media frame → cheap μ-law decode only.
    * ``update_diarization(turns)`` whenever Deepgram emits a
      ``Results`` event carrying diarized words (marshalled onto the
      writer thread by the caller). Overlapping turns replace older
      ones for the same time range; the internal timeline stays
      monotonic.
    * ``maybe_begin_snapshot()`` whenever you're willing to pay the
      compute cost → returns an immutable :class:`SnapshotJob` when the
      recompute interval has elapsed, or None otherwise. Run the job on
      an executor thread.
    * ``note_overrun()`` / ``note_ok()`` after each job — overruns back
      off the recompute cadence (doubling, capped) so a slow Praat
      never piles work up; a completed job resets it.
    """

    # Synthetic id used when no diarization timeline has arrived yet.
    # The scanner treats it identically to a real per-speaker row.
    WHOLE_WINDOW_SPEAKER_ID = "window"

    # Cadence backoff cap — with recompute_every_sec=3.0 this allows
    # 3 → 6 → 12 and stops there.
    MAX_BACKOFF_SEC = 12.0

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
        self._backoff_multiplier: float = 1.0

    # ── Frame intake ─────────────────────────────────────────────────

    def feed(self, payload: bytes) -> None:
        if not payload:
            return
        try:
            pcm = ulaw_to_pcm16(payload)
        except Exception:
            logger.debug(
                "ulaw decode failed in live paralinguistic window", exc_info=True
            )
            return
        offset = time.time() - self._call_start
        self._chunks.append(_Chunk(pcm=pcm, offset=offset))
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

    @property
    def current_interval_sec(self) -> float:
        """Effective recompute interval after cadence backoff."""
        return min(
            self.recompute_every_sec * self._backoff_multiplier,
            max(self.MAX_BACKOFF_SEC, self.recompute_every_sec),
        )

    def note_overrun(self) -> None:
        """A snapshot blew its deadline — widen the cadence (capped)."""
        widened = self.recompute_every_sec * self._backoff_multiplier * 2.0
        if widened <= max(self.MAX_BACKOFF_SEC, self.recompute_every_sec):
            self._backoff_multiplier *= 2.0

    def note_ok(self) -> None:
        """A snapshot completed within budget — restore the cadence."""
        self._backoff_multiplier = 1.0

    def maybe_begin_snapshot(self) -> Optional[SnapshotJob]:
        """Return an immutable :class:`SnapshotJob` if the recompute
        interval has elapsed and the buffer holds enough audio.

        Must be called on the writer thread. The job carries copies of
        the audio and the diarization timeline, so the window is free
        to keep mutating while the job runs elsewhere.
        """
        now = time.time()
        if now - self._last_snapshot_at < self.current_interval_sec:
            return None
        self._last_snapshot_at = now
        if not self._chunks:
            return None

        window_start_offset = self._chunks[0].offset
        window_end_offset = self._chunks[-1].offset
        duration = max(0.0, window_end_offset - window_start_offset)
        if duration < 1.5:
            return None

        # ``join`` and the per-turn reconstruction are the copies that
        # make the job immutable — DiarTurn instances are rebuilt
        # because the writer mutates ``end`` in place when collapsing.
        return SnapshotJob(
            pcm=b"".join(c.pcm for c in self._chunks),
            sample_rate=self.sample_rate,
            window_start_offset=window_start_offset,
            window_end_offset=window_end_offset,
            turns=[
                DiarTurn(start=t.start, end=t.end, speaker=t.speaker)
                for t in self._diar_turns
            ],
            extractor=self._extractor,
        )

    def maybe_snapshot(self) -> Optional[ParalinguisticFeatures]:
        """Blocking convenience: begin + run in one call.

        Kept for the replay harness and tests; the live path uses
        ``maybe_begin_snapshot`` + executor so Praat never runs on the
        writer thread.
        """
        job = self.maybe_begin_snapshot()
        if job is None:
            return None
        return job.run()

    def _build_segments(
        self,
        *,
        window_start_offset: float,
        window_end_offset: float,
    ) -> List[SpeakerAudioSegment]:
        return _build_segments_from_turns(
            self._diar_turns,
            window_start_offset=window_start_offset,
            window_end_offset=window_end_offset,
        )


def _build_segments_from_turns(
    turns: List[DiarTurn],
    *,
    window_start_offset: float,
    window_end_offset: float,
) -> List[SpeakerAudioSegment]:
    """Turn a diarization timeline into :class:`SpeakerAudioSegment`
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
    if not turns:
        return [
            SpeakerAudioSegment(
                speaker_id=LiveParalinguisticWindow.WHOLE_WINDOW_SPEAKER_ID,
                start=0.0,
                end=window_end_offset - window_start_offset,
            )
        ]

    segments: List[SpeakerAudioSegment] = []
    for turn in turns:
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
                speaker_id=LiveParalinguisticWindow.WHOLE_WINDOW_SPEAKER_ID,
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
    "SnapshotJob",
    "diar_turns_from_deepgram_words",
]
