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
    compute_lsm_for_transcript,
    compute_lsm_pair,
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
