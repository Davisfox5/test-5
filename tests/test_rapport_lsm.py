"""LSM rapport-gauge tests.

Pinning the function-word categories, the per-pair LSM math, and the
transcript ingestion's role-classification heuristics.
"""

from __future__ import annotations

import pytest

from backend.app.services.rapport_lsm import (
    FUNCTION_WORDS,
    LSM_EPSILON,
    attach_rapport,
    attach_vocal_accommodation,
    compute_lsm_for_transcript,
    compute_lsm_pair,
    compute_vocal_accommodation,
)


def test_function_word_categories_present():
    assert "articles" in FUNCTION_WORDS
    assert "personal_pronouns" in FUNCTION_WORDS
    assert "auxiliary_verbs" in FUNCTION_WORDS
    # Spot-check membership in case anyone accidentally rewrites these
    assert "the" in FUNCTION_WORDS["articles"]
    assert "we" in FUNCTION_WORDS["personal_pronouns"]
    assert "would" in FUNCTION_WORDS["auxiliary_verbs"]


def test_compute_lsm_pair_identical_text_is_one():
    text = "The team is working on the new model and they think it is great."
    r = compute_lsm_pair(text, text)
    assert r is not None
    assert r["overall"] == pytest.approx(1.0, abs=1e-3)


def test_compute_lsm_pair_completely_different_categories():
    """Heavy article use vs heavy negation use → low overall LSM."""
    a = "The the the the the a the a an the the"
    b = "not no never not no never nothing nobody nowhere not no"
    r = compute_lsm_pair(a, b)
    assert r is not None
    # Overall should be ~mean of categories where one side is 1.0 and
    # the other is 0.0 → 0 for those categories. Other categories are
    # 1.0 (both zero, so 1 - 0/(0+0+ε) ≈ 1). Mean is around 0.7.
    assert r["overall"] < 0.85


def test_compute_lsm_pair_empty_returns_none():
    assert compute_lsm_pair("", "anything at all") is None
    assert compute_lsm_pair("anything at all", "") is None
    assert compute_lsm_pair("", "") is None


def test_compute_lsm_for_transcript_role_inference():
    transcript = [
        {"speaker": "Customer", "text": "I am not sure about the price."},
        {"speaker": "Agent", "text": "I can show you how the bigger plan saves money."},
        {"speaker": "Customer", "text": "I would love to see the numbers."},
    ]
    r = compute_lsm_for_transcript(transcript)
    assert r is not None
    assert 0.0 <= r["overall"] <= 1.0


def test_compute_lsm_for_transcript_speaker_a_b_fallback():
    transcript = [
        {"speaker": "Speaker A", "text": "Are you considering the larger model?"},
        {"speaker": "Speaker B", "text": "I am considering it but I am unsure."},
    ]
    r = compute_lsm_for_transcript(transcript)
    assert r is not None


def test_compute_lsm_for_transcript_one_side_only_returns_none():
    transcript = [
        {"speaker": "agent", "text": "Hello, this is your account manager."},
    ]
    assert compute_lsm_for_transcript(transcript) is None


def test_compute_lsm_for_transcript_empty_returns_none():
    assert compute_lsm_for_transcript([]) is None


def test_attach_rapport_writes_rapport_key():
    transcript = [
        {"speaker": "agent", "text": "I think we have a great option for you."},
        {"speaker": "customer", "text": "I would like to see the numbers."},
    ]
    insights = {}
    out = attach_rapport(insights, transcript)
    assert out is not None
    assert "rapport" in insights
    assert "lsm_overall" in insights["rapport"]
    assert "lsm_by_category" in insights["rapport"]
    assert 0.0 <= insights["rapport"]["lsm_overall"] <= 1.0


def test_attach_rapport_no_op_on_single_speaker():
    transcript = [
        {"speaker": "agent", "text": "Just me talking here."},
    ]
    insights = {}
    out = attach_rapport(insights, transcript)
    assert out is None
    assert "rapport" not in insights


def test_lsm_epsilon_unchanged():
    """Pennebaker's original ε is 0.0001. Don't drift."""
    assert LSM_EPSILON == 0.0001


# ── Vocal accommodation (Phase 2) ────────────────────────────────────


def _para(rep_block: dict, cust_block: dict) -> dict:
    """Build a paralinguistic block in the shape ``compute_vocal_accommodation`` expects."""
    return {
        "available": True,
        "per_speaker": {"agent": rep_block, "customer": cust_block},
    }


def test_compute_vocal_accommodation_perfect_mirror_is_one():
    rep = {
        "pitch_hz_p50": 150.0,
        "intensity_db_p50": 65.0,
        "speaking_rate_syll_per_sec": 3.5,
        "pause_rate_per_min": 4.0,
    }
    accom = compute_vocal_accommodation(_para(rep, dict(rep)))
    assert accom is not None
    assert accom["overall"] == pytest.approx(1.0, abs=1e-3)


def test_compute_vocal_accommodation_diverges_with_distance():
    rep = {
        "pitch_hz_p50": 100.0,
        "intensity_db_p50": 60.0,
        "speaking_rate_syll_per_sec": 2.0,
        "pause_rate_per_min": 1.0,
    }
    cust = {
        "pitch_hz_p50": 250.0,
        "intensity_db_p50": 80.0,
        "speaking_rate_syll_per_sec": 6.0,
        "pause_rate_per_min": 8.0,
    }
    accom = compute_vocal_accommodation(_para(rep, cust))
    assert accom is not None
    # Mismatched poles → overall well below 1.0.
    assert accom["overall"] < 0.85


def test_compute_vocal_accommodation_returns_none_when_unavailable():
    assert compute_vocal_accommodation(None) is None
    assert compute_vocal_accommodation({"available": False}) is None
    assert (
        compute_vocal_accommodation({"available": True, "per_speaker": {}})
        is None
    )


def test_compute_vocal_accommodation_skips_when_features_missing_one_side():
    rep = {"pitch_hz_p50": 150.0}  # only one feature on rep side
    cust = {"intensity_db_p50": 70.0}  # disjoint feature on customer
    # No overlapping feature with values on both sides → None.
    assert compute_vocal_accommodation(_para(rep, cust)) is None


def test_attach_vocal_accommodation_blends_with_lsm_overall():
    insights = {"rapport": {"lsm_overall": 0.80, "overall": 0.80}}
    rep = {"pitch_hz_p50": 150.0, "intensity_db_p50": 65.0}
    cust = {"pitch_hz_p50": 150.0, "intensity_db_p50": 65.0}
    out = attach_vocal_accommodation(insights, _para(rep, cust))
    assert out is not None
    # Both halves at 1.0 (perfect mirror) and 0.80 → composite is mean.
    assert insights["rapport"]["overall"] == pytest.approx(0.90, abs=0.01)
    assert insights["rapport"]["vocal_accommodation"]["overall"] == pytest.approx(
        1.0, abs=1e-3
    )


def test_attach_vocal_accommodation_no_op_without_paralinguistic_block():
    insights = {"rapport": {"lsm_overall": 0.7, "overall": 0.7}}
    out = attach_vocal_accommodation(insights, None)
    assert out is None
    # LSM-only state untouched.
    assert "vocal_accommodation" not in insights["rapport"]
    assert insights["rapport"]["overall"] == 0.7
