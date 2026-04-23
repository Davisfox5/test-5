"""Tests for :class:`FeatureExtractor` and its component computations.

Uses hand-built :class:`Segment` fixtures so we can verify each formula
(patience, interactivity, turn entropy, LSM, back-channels, pauses,
laughter) in isolation.  These are the highest-value deterministic
features and they're tested exhaustively because the orchestrator and
score engine both depend on them being correct.
"""

from typing import List

import pytest

from backend.app.services.feature_extractors import (
    FeatureExtractor,
    _category_counts,
    _looks_like_backchannel,
    _tokens,
)
from backend.app.services.transcription import Segment


def _seg(start: float, end: float, text: str, speaker: str = "agent") -> Segment:
    return Segment(start=start, end=end, text=text, speaker_id=speaker, confidence=1.0)


def _long_text(prefix: str) -> str:
    """Produce ≥120 tokens of deterministic filler for LSM stability tests."""
    words = [
        "we", "have", "been", "looking", "at", "your", "proposal",
        "and", "i", "think", "it", "is", "interesting", "but", "we",
        "need", "to", "understand", "the", "pricing", "better", "so",
        "that", "we", "can", "bring", "it", "back", "to", "the",
    ]
    return prefix + " " + " ".join(words * 6)


# ── Small helpers ────────────────────────────────────────────────────────


def test_category_counts_matches_lsm_vocabulary():
    tokens = ["we", "have", "the", "pricing", "discussion"]
    counts = _category_counts(tokens)
    assert counts["pronouns_personal"] == 1  # "we"
    assert counts["auxiliary_verbs"] == 1  # "have"
    assert counts["articles"] == 1  # "the"


def test_tokens_strips_punctuation_and_lowercases():
    assert _tokens("Hello, World!  It's great.") == ["hello", "world", "it's", "great"]


def test_looks_like_backchannel_recognizes_short_listener_utterance():
    assert _looks_like_backchannel("yeah")
    assert _looks_like_backchannel("mm-hmm")
    assert _looks_like_backchannel("uh-huh, right")
    assert not _looks_like_backchannel(
        "yeah I think that's a great point because it proves your case"
    )
    assert not _looks_like_backchannel("")


# ── Extractor end-to-end ─────────────────────────────────────────────────


def test_extract_returns_zeroed_fields_on_empty_segments():
    out = FeatureExtractor().extract([])
    assert out["total_turns"] == 0
    assert out["stakeholder_count"] == 0
    assert out["linguistic_style_match"] is None


def test_patience_is_median_gap_before_agent_turn():
    # customer [0, 2] → gap 0.5 → agent [2.5, 3]
    # customer [3.5, 5] → gap 0.8 → agent [5.8, 6]
    # Should yield median pre-agent gap of 0.65.
    segments = [
        _seg(0.0, 2.0, "Hello there", speaker="customer"),
        _seg(2.5, 3.0, "Hi", speaker="agent"),
        _seg(3.5, 5.0, "Follow-up question", speaker="customer"),
        _seg(5.8, 6.0, "Sure", speaker="agent"),
    ]
    out = FeatureExtractor().extract(segments, agent_speaker_ids=["agent"])
    assert out["patience_sec"] == pytest.approx(0.65, abs=1e-3)


def test_interactivity_counts_switches_per_minute():
    # 4 turns across 6 seconds → 3 switches / 0.1 min = 30/min.
    segments = [
        _seg(0.0, 1.5, "A", speaker="agent"),
        _seg(1.5, 3.0, "B", speaker="customer"),
        _seg(3.0, 4.5, "C", speaker="agent"),
        _seg(4.5, 6.0, "D", speaker="customer"),
    ]
    out = FeatureExtractor().extract(segments, agent_speaker_ids=["agent"])
    assert out["interactivity_per_min"] == pytest.approx(30.0, abs=0.01)
    assert out["total_turns"] == 4


def test_turn_entropy_is_zero_for_single_speaker_and_one_for_balanced():
    # Single speaker — entropy over one category = 0.
    single = [_seg(0.0, 1.0, "Hi"), _seg(1.0, 2.0, "Again")]
    assert FeatureExtractor().extract(single)["turn_entropy"] == 0.0

    # Two speakers, perfectly balanced turns → entropy = 1.0.
    balanced = [
        _seg(0.0, 1.0, "A", speaker="agent"),
        _seg(1.0, 2.0, "B", speaker="customer"),
        _seg(2.0, 3.0, "C", speaker="agent"),
        _seg(3.0, 4.0, "D", speaker="customer"),
    ]
    assert FeatureExtractor().extract(balanced)["turn_entropy"] == pytest.approx(1.0)


def test_longest_customer_story_mirrors_longest_monologue():
    segments = [
        _seg(0.0, 5.0, "ok", speaker="agent"),
        _seg(5.0, 45.0, "long customer monologue", speaker="customer"),
        _seg(45.0, 47.0, "i see", speaker="agent"),
    ]
    out = FeatureExtractor().extract(segments, agent_speaker_ids=["agent"])
    assert out["longest_customer_story_sec"] == pytest.approx(40.0)
    assert out["longest_monologue_sec"]["agent"] == pytest.approx(5.0)


def test_back_channel_rate_counts_listener_acknowledgements():
    segments = [
        _seg(0.0, 20.0, "So the main pricing issue is this long explanation.", speaker="customer"),
        _seg(20.0, 20.5, "yeah", speaker="agent"),
        _seg(20.5, 21.0, "mm-hmm", speaker="agent"),
        _seg(21.0, 21.5, "right", speaker="agent"),
    ]
    out = FeatureExtractor().extract(segments, agent_speaker_ids=["agent"])
    # Three back-channels by the agent across ~0.025 minutes of agent
    # talk time — the rate is large by construction; just assert it's
    # positive and consistent.
    assert out["back_channel_rate_per_min"]["agent"] > 0
    assert out["back_channel_rate_per_min"]["customer"] == 0


def test_pause_distribution_reports_percentiles_and_max():
    segments = [
        _seg(0.0, 1.0, "a", speaker="a"),
        _seg(1.2, 2.0, "b", speaker="b"),  # gap 0.2
        _seg(3.0, 4.0, "c", speaker="a"),  # gap 1.0
        _seg(10.0, 11.0, "d", speaker="b"),  # gap 6.0
    ]
    dist = FeatureExtractor().extract(segments)["pause_distribution_sec"]
    assert dist["max"] == pytest.approx(6.0)
    assert dist["p90"] >= dist["p50"]


def test_laughter_events_detected_from_transcript():
    segments = [
        _seg(0.0, 1.0, "haha that's a good one", speaker="agent"),
        _seg(1.0, 2.0, "[laughter]", speaker="customer"),
        _seg(2.0, 3.0, "lol yeah", speaker="agent"),
    ]
    out = FeatureExtractor().extract(segments)
    assert out["laughter_events"] == 3


def test_lsm_returns_none_when_tokens_too_few():
    short = [
        _seg(0.0, 1.0, "hi there", speaker="agent"),
        _seg(1.0, 2.0, "hello", speaker="customer"),
    ]
    assert FeatureExtractor().extract(short)["linguistic_style_match"] is None


def test_lsm_is_between_zero_and_one_when_enough_tokens():
    segments = [
        _seg(0.0, 60.0, _long_text("agent side:"), speaker="agent"),
        _seg(60.0, 120.0, _long_text("customer side:"), speaker="customer"),
    ]
    out = FeatureExtractor().extract(segments, agent_speaker_ids=["agent"])
    lsm = out["linguistic_style_match"]
    assert lsm is not None
    assert 0.0 <= lsm <= 1.0


def test_stakeholder_count_counts_distinct_speakers():
    segments = [
        _seg(0.0, 1.0, "a", speaker="agent"),
        _seg(1.0, 2.0, "b", speaker="customer1"),
        _seg(2.0, 3.0, "c", speaker="customer2"),
    ]
    out = FeatureExtractor().extract(segments, agent_speaker_ids=["agent"])
    assert out["stakeholder_count"] == 3
