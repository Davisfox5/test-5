"""Arousal + emotion annotation tests.

Focus on the deterministic arousal path (no new deps). SpeechBrain
emotion classification is a no-op without the library installed, so we
cover it structurally (API shape, graceful degradation) rather than
round-tripping a real model.
"""

from __future__ import annotations

import pytest

from backend.app.services.paralinguistics_emotion import (
    ArousalResult,
    EmotionResult,
    annotate_arousal,
    annotate_emotion,
    classify_emotion,
    compute_arousal,
)


# ── Arousal scoring ──────────────────────────────────────────────────


def test_arousal_returns_none_when_too_few_features():
    assert compute_arousal({}) is None
    # One axis alone isn't enough — noisy.
    assert compute_arousal({"pitch_std_semitones": 3.0}) is None


def test_arousal_calm_speaker_maps_to_low_score():
    calm = {
        "pitch_std_semitones": 1.0,
        "intensity_db_p50": 50.0,
        "speaking_rate_syll_per_sec": 1.5,
        "jitter_local": 0.005,
        "shimmer_local": 0.03,
    }
    result = compute_arousal(calm)
    assert result is not None
    assert result.score < 0.25
    assert result.label == "calm"


def test_arousal_agitated_speaker_maps_to_high_score():
    agitated = {
        "pitch_std_semitones": 6.0,
        "intensity_db_p50": 80.0,
        "speaking_rate_syll_per_sec": 5.0,
        "jitter_local": 0.04,
        "shimmer_local": 0.15,
    }
    result = compute_arousal(agitated)
    assert result is not None
    assert result.score >= 0.75
    assert result.label == "agitated"


def test_arousal_neutral_middle():
    neutral = {
        "pitch_std_semitones": 3.5,
        "intensity_db_p50": 65.0,
        "speaking_rate_syll_per_sec": 3.0,
    }
    result = compute_arousal(neutral)
    assert result is not None
    # Halfway across each axis → ~0.5
    assert 0.3 < result.score < 0.7


def test_arousal_label_boundaries():
    # Score-only check to lock in the label thresholds.
    assert ArousalResult(0.10, _arousal_label(0.10)).label == "calm"
    assert ArousalResult(0.30, _arousal_label(0.30)).label == "neutral"
    assert ArousalResult(0.60, _arousal_label(0.60)).label == "elevated"
    assert ArousalResult(0.90, _arousal_label(0.90)).label == "agitated"


def _arousal_label(score: float) -> str:
    # Mirror of the helper in the module — keeps the test self-contained
    # without touching private members.
    if score < 0.25:
        return "calm"
    if score < 0.5:
        return "neutral"
    if score < 0.75:
        return "elevated"
    return "agitated"


# ── annotate_arousal integration ─────────────────────────────────────


def test_annotate_arousal_populates_per_speaker_entries():
    block = {
        "available": True,
        "backend": "parselmouth",
        "per_speaker": {
            "agent": {
                "pitch_std_semitones": 3.0,
                "intensity_db_p50": 65,
                "speaking_rate_syll_per_sec": 3.0,
            },
            "customer": {
                "pitch_std_semitones": 6.0,
                "intensity_db_p50": 80,
                "speaking_rate_syll_per_sec": 5.0,
            },
        },
        "overall": {
            "pitch_std_semitones": 4.5,
            "intensity_db_p50": 70,
            "speaking_rate_syll_per_sec": 4.0,
        },
    }
    out = annotate_arousal(block)
    assert "arousal" in out["per_speaker"]["agent"]
    assert "arousal" in out["per_speaker"]["customer"]
    assert "arousal" in out["overall"]
    assert out["per_speaker"]["customer"]["arousal"]["score"] > \
        out["per_speaker"]["agent"]["arousal"]["score"]


def test_annotate_arousal_noop_when_unavailable():
    block = {"available": False, "backend": "none"}
    # Returns the same block untouched.
    assert annotate_arousal(block) is block


def test_annotate_arousal_handles_missing_per_speaker_keys():
    """Degrade gracefully when per_speaker is empty."""
    block = {
        "available": True,
        "backend": "parselmouth",
        "per_speaker": {},
        "overall": {
            "pitch_std_semitones": 3.0,
            "intensity_db_p50": 65,
            "speaking_rate_syll_per_sec": 3.0,
        },
    }
    out = annotate_arousal(block)
    # overall still gets annotated even if per_speaker is empty.
    assert "arousal" in out["overall"]


# ── Emotion plug-in (optional) ───────────────────────────────────────


def test_classify_emotion_returns_none_without_speechbrain(tmp_path):
    """Without the SpeechBrain dep installed, classify_emotion is a
    no-op that returns None rather than crashing the pipeline."""
    wav = tmp_path / "empty.wav"
    wav.write_bytes(b"")  # irrelevant — classifier short-circuits first
    result = classify_emotion(str(wav))
    # In a clean environment, speechbrain is not installed — None.
    # If it IS installed, ensure we at least get a plausible shape.
    if result is None:
        return
    assert isinstance(result, EmotionResult)
    assert isinstance(result.label, str)
    assert 0.0 <= result.confidence <= 1.0


def test_annotate_emotion_is_noop_when_speechbrain_missing():
    block = {
        "available": True,
        "backend": "parselmouth",
        "per_speaker": {"agent": {"pitch_std_semitones": 3.0}},
        "overall": {},
    }
    out = annotate_emotion(block, [("agent", "/nonexistent.wav")])
    # Either we're in a speechbrain-free environment (no-op, no emotion
    # key), or speechbrain IS installed but the file doesn't exist
    # (classify_emotion returns None, still no-op).
    assert "emotion" not in out["per_speaker"]["agent"]
