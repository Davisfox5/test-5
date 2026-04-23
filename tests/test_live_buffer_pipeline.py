"""Tests for the live-to-pipeline buffer converter used on session end.

The live WebSocket stores segments with a wall-clock timestamp; the batch
pipeline expects relative start/end seconds plus speaker_id/confidence.
"""

from backend.app.api.websocket import _buffer_to_pipeline_segments


def test_empty_buffer_returns_empty_list():
    assert _buffer_to_pipeline_segments([]) == []


def test_single_segment_gets_zero_start_and_estimated_end():
    out = _buffer_to_pipeline_segments(
        [{"text": "hi", "speaker": 0, "timestamp": 1000.0}]
    )
    assert len(out) == 1
    assert out[0]["start"] == 0.0
    assert out[0]["end"] == 2.0  # single-segment fallback
    assert out[0]["speaker_id"] == "0"
    assert out[0]["text"] == "hi"
    assert out[0]["confidence"] == 1.0


def test_multiple_segments_are_base_relative_and_chained():
    buf = [
        {"text": "hello",     "speaker": 0, "timestamp": 100.0},
        {"text": "how are u", "speaker": 1, "timestamp": 102.5},
        {"text": "great",     "speaker": 0, "timestamp": 105.0},
    ]
    out = _buffer_to_pipeline_segments(buf)
    assert [s["start"] for s in out] == [0.0, 2.5, 5.0]
    # First two ends chain off the next segment's start.
    assert out[0]["end"] == 2.5
    assert out[1]["end"] == 5.0
    # Last segment falls back to +2s.
    assert out[2]["end"] == 7.0
    assert [s["speaker_id"] for s in out] == ["0", "1", "0"]


def test_missing_timestamp_uses_index_fallback():
    buf = [
        {"text": "a", "speaker": 0, "timestamp": None},
        {"text": "b", "speaker": 0, "timestamp": None},
    ]
    out = _buffer_to_pipeline_segments(buf)
    # base_ts = 0, first ts not numeric → start is 0 (i=0) and 2 (i=1)
    assert out[0]["start"] == 0.0
    assert out[1]["start"] == 2.0


def test_unknown_speaker_becomes_string_placeholder():
    out = _buffer_to_pipeline_segments(
        [{"text": "hi", "speaker": None, "timestamp": 10.0}]
    )
    assert out[0]["speaker_id"] == "Unknown"
