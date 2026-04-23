"""Tests for Deepgram live diarization → per-speaker paralinguistic
timeline in :mod:`paralinguistics_live`.

These exercise the pure-Python logic — timeline merging, per-speaker
segment construction, Deepgram-word-to-turn collapsing. Parselmouth
isn't required; we stub the extractor when we need to inspect the
segments the window would feed to it.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from backend.app.services.paralinguistics import SpeakerAudioSegment
from backend.app.services.paralinguistics_live import (
    DiarTurn,
    LiveParalinguisticWindow,
    _collapse_adjacent_turns,
    diar_turns_from_deepgram_words,
)


# ── Word-stream → turn collapsing ────────────────────────────────────


def test_diar_turns_collapse_consecutive_same_speaker():
    words = [
        {"speaker": 0, "start": 0.0, "end": 0.3},
        {"speaker": 0, "start": 0.4, "end": 0.7},
        {"speaker": 1, "start": 0.8, "end": 1.0},
        {"speaker": 0, "start": 1.1, "end": 1.4},
    ]
    turns = diar_turns_from_deepgram_words(words)
    assert [t.speaker for t in turns] == ["0", "1", "0"]
    assert turns[0].start == 0.0 and turns[0].end == 0.7
    assert turns[1].start == 0.8 and turns[1].end == 1.0
    assert turns[2].start == 1.1 and turns[2].end == 1.4


def test_diar_turns_handles_empty_and_malformed():
    assert diar_turns_from_deepgram_words([]) == []
    # Missing fields → skipped, not crash.
    turns = diar_turns_from_deepgram_words(
        [{"speaker": 0}, {"start": 0.0, "end": 1.0}]
    )
    assert turns == []


def test_diar_turns_accepts_call_start_offset():
    words = [{"speaker": 0, "start": 0.0, "end": 0.5}]
    turns = diar_turns_from_deepgram_words(words, call_start_offset=10.0)
    assert turns[0].start == 10.0
    assert turns[0].end == 10.5


# ── Timeline merging inside the window ──────────────────────────────


def test_update_diarization_drops_zero_length_turns():
    w = LiveParalinguisticWindow()
    w.update_diarization(
        [
            DiarTurn(start=0.0, end=0.0, speaker="0"),  # ignored
            DiarTurn(start=0.5, end=1.0, speaker="0"),
        ]
    )
    assert len(w._diar_turns) == 1


def test_update_diarization_collapses_adjacent_same_speaker():
    w = LiveParalinguisticWindow()
    w.update_diarization(
        [
            DiarTurn(start=0.0, end=1.0, speaker="0"),
            DiarTurn(start=1.05, end=2.0, speaker="0"),  # 50 ms gap → merge
            DiarTurn(start=2.5, end=3.0, speaker="1"),
        ]
    )
    assert len(w._diar_turns) == 2
    assert w._diar_turns[0].start == 0.0
    assert w._diar_turns[0].end == 2.0
    assert w._diar_turns[0].speaker == "0"


def test_collapse_keeps_distinct_speakers_back_to_back():
    collapsed = _collapse_adjacent_turns(
        [
            DiarTurn(start=0.0, end=1.0, speaker="0"),
            DiarTurn(start=1.0, end=2.0, speaker="1"),
        ]
    )
    assert len(collapsed) == 2


def test_update_diarization_preserves_ordering_when_turns_arrive_unsorted():
    w = LiveParalinguisticWindow()
    w.update_diarization(
        [
            DiarTurn(start=2.0, end=3.0, speaker="0"),
            DiarTurn(start=0.0, end=1.0, speaker="0"),
        ]
    )
    starts = [t.start for t in w._diar_turns]
    assert starts == sorted(starts)


# ── Per-speaker segment construction ────────────────────────────────


def _fill_with_silence(window: LiveParalinguisticWindow, seconds: float) -> None:
    """Push enough silence frames into the window to cover ``seconds``
    of call time. Uses the synthetic monkeypatched clock via
    direct mutation of _call_start so we don't wait on wall clock."""
    # Each μ-law silence byte = 1 sample at 8 kHz; 160 bytes = 20 ms.
    bytes_per_frame = 160
    frames_per_sec = 50
    total_frames = int(seconds * frames_per_sec)
    for i in range(total_frames):
        with patch(
            "backend.app.services.paralinguistics_live.time"
        ) as mock_time:
            mock_time.time.return_value = window._call_start + (i + 1) * (
                1.0 / frames_per_sec
            )
            window.feed(b"\xff" * bytes_per_frame)


def test_build_segments_returns_whole_window_when_no_diarization():
    w = LiveParalinguisticWindow(window_sec=10.0)
    _fill_with_silence(w, 3.0)
    segments = w._build_segments(
        window_start_offset=w._chunks[0].offset,
        window_end_offset=w._chunks[-1].offset,
    )
    assert len(segments) == 1
    assert segments[0].speaker_id == LiveParalinguisticWindow.WHOLE_WINDOW_SPEAKER_ID


def test_build_segments_slices_by_diarization_turns():
    w = LiveParalinguisticWindow(window_sec=10.0)
    _fill_with_silence(w, 4.0)
    first = w._chunks[0].offset
    last = w._chunks[-1].offset
    # Two turns covering most of the window.
    w.update_diarization(
        [
            DiarTurn(start=first, end=first + 1.5, speaker="0"),
            DiarTurn(start=first + 2.0, end=last, speaker="1"),
        ]
    )
    segments = w._build_segments(
        window_start_offset=first,
        window_end_offset=last,
    )
    assert len(segments) == 2
    assert {s.speaker_id for s in segments} == {"0", "1"}
    # Segments should be window-relative (start at 0), not call-relative.
    assert segments[0].start == 0.0
    assert segments[0].end == pytest.approx(1.5, abs=0.05)


def test_build_segments_falls_back_when_turns_do_not_overlap_window():
    w = LiveParalinguisticWindow(window_sec=10.0)
    _fill_with_silence(w, 2.0)
    first = w._chunks[0].offset
    last = w._chunks[-1].offset
    # Turns from the distant past — older than anything in the buffer.
    w.update_diarization(
        [DiarTurn(start=first - 100.0, end=first - 90.0, speaker="0")]
    )
    segments = w._build_segments(
        window_start_offset=first,
        window_end_offset=last,
    )
    assert len(segments) == 1
    assert segments[0].speaker_id == LiveParalinguisticWindow.WHOLE_WINDOW_SPEAKER_ID


def test_feed_trims_stale_diarization_turns():
    w = LiveParalinguisticWindow(window_sec=1.0)
    # Seed a turn well inside our "last 1 s" window, then advance the
    # clock so feed() prunes anything older.
    w.update_diarization([DiarTurn(start=0.0, end=0.1, speaker="0")])
    with patch("backend.app.services.paralinguistics_live.time") as mock_time:
        mock_time.time.return_value = w._call_start + 5.0  # 5 s later
        w.feed(b"\xff" * 160)
    # The old turn (ended at 0.1 s) should be gone — it's >1s old now.
    assert w._diar_turns == []
