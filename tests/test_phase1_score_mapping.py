"""Phase 1 + Phase 3 scoring foundation tests.

Pinning the bucket → number map and the deterministic rubric so the
prompt change can't silently drift the numeric values downstream
analytics depend on.
"""

from __future__ import annotations

from backend.app.services.evidence_scoring import attach_rubric, compute_rubric
from backend.app.services.score_classification import (
    SCORE_FIELD_CLASSIFICATION,
    category_of,
)
from backend.app.services.score_mapping import (
    derive_numeric_scores,
    map_risk,
    map_script_adherence,
    map_sentiment,
)


def test_bucket_to_score_canonical_values():
    assert map_sentiment("positive") == 8.5
    assert map_sentiment("neutral") == 6.0
    assert map_sentiment("mixed") == 4.5
    assert map_sentiment("negative") == 2.5
    assert map_sentiment("nonsense") is None
    assert map_sentiment(None) is None

    assert map_risk("high") == 0.85
    assert map_risk("medium") == 0.55
    assert map_risk("low") == 0.25
    assert map_risk("none") == 0.05

    assert map_script_adherence("high") == 92.0
    assert map_script_adherence("failing") == 30.0


def test_derive_numeric_scores_populates_legacy_fields():
    insights = {
        "sentiment_overall": "positive",
        "churn_risk_signal": "medium",
        "upsell_signal": "low",
        "coaching": {"script_adherence_band": "medium"},
    }
    derive_numeric_scores(insights)
    assert insights["sentiment_score"] == 8.5
    assert insights["churn_risk"] == 0.55
    assert insights["upsell_score"] == 0.25
    assert insights["coaching"]["script_adherence_score"] == 75.0


def test_derive_numeric_scores_leaves_unknown_buckets_alone():
    """An LLM that invents a new bucket label shouldn't blow up the
    numeric field; the previous value (or absence) sticks."""
    insights = {
        "sentiment_overall": "ambivalent_with_a_twist",
        "churn_risk_signal": "high",
    }
    derive_numeric_scores(insights)
    assert "sentiment_score" not in insights
    assert insights["churn_risk"] == 0.85


def test_compute_rubric_zero_evidence_is_zero():
    r = compute_rubric({})
    assert r == {
        "discovery_quality": 0.0,
        "commitment_strength": 0.0,
        # No objections raised → handling rate is "neutral" (no claim)
        "objection_resolution_rate": 0.5,
        # Win likelihood weights the three above plus competitor
        # penalty: 0.4*0 + 0.3*0 + 0.3*0.5 - 0 = 0.15
        "win_likelihood": 0.15,
    }


def test_compute_rubric_full_call():
    r = compute_rubric(
        {
            "objection_count": 3,
            "unresolved_objection_count": 1,
            "commitment_count": 2,
            "discovery_questions": 6,
            "competitor_mention_count": 1,
        }
    )
    assert r["discovery_quality"] == 0.75  # 6 / 8 cap
    assert r["commitment_strength"] == round(2 / 3, 3)
    assert r["objection_resolution_rate"] == round(2 / 3, 3)
    # 0.4*0.75 + 0.3*0.667 + 0.3*0.667 - 0.10*1 = 0.3 + 0.2 + 0.2 - 0.1
    assert 0.55 <= r["win_likelihood"] <= 0.65


def test_attach_rubric_no_op_without_evidence():
    insights = {"sentiment_overall": "positive"}
    out = attach_rubric(insights)
    assert out is None
    assert "rubric" not in insights


def test_attach_rubric_writes_rubric_key():
    insights = {
        "evidence": {
            "objection_count": 1,
            "unresolved_objection_count": 0,
            "commitment_count": 1,
            "discovery_questions": 4,
            "competitor_mention_count": 0,
        }
    }
    out = attach_rubric(insights)
    assert out is not None
    assert "rubric" in insights
    assert insights["rubric"]["discovery_quality"] == 0.5


def test_score_classification_covers_all_bucket_fields():
    """Anything we mapped from a bucket should also be classified, so
    we don't ship a numeric field whose category is implicit."""
    bucket_emitted = [
        "sentiment_overall",
        "churn_risk_signal",
        "upsell_signal",
        "churn_risk",
        "upsell_score",
        "sentiment_score",
        "coaching.script_adherence_band",
        "coaching.script_adherence_score",
    ]
    for f in bucket_emitted:
        assert f in SCORE_FIELD_CLASSIFICATION, f"missing classification: {f}"


def test_category_of_unknown_field_is_subjective_default():
    """Defaulting unknown to subjective is the safe choice — don't let
    a stray LLM-emitted field sneak into "measurement" analytics."""
    assert category_of("never_seen_before") == "subjective"
