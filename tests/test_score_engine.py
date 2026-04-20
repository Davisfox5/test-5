"""Tests for the score engine — composite scoring + top-K factor output.

These tests exercise the full contract: the ``value`` is in range, the
``top_factors`` come back ranked by contribution, recommendations are
only produced for negative factors, and the public projection strips
internal weights / feature ids.
"""

import pytest

from backend.app.services.score_engine import (
    CompositeScorer,
    ScoreResult,
    WeightedFeature,
    _trajectory_slope,
    default_churn_scorer,
    default_health_scorer,
    default_sentiment_scorer,
    flatten_features_for_churn,
    flatten_features_for_sentiment,
)


def _toy_scorer(**kw) -> CompositeScorer:
    return CompositeScorer(
        name=kw.get("name", "toy"),
        version="v1",
        intercept=50.0,
        features=[
            WeightedFeature(
                "patience_sec", weight=5.0, baseline_mean=0.7, baseline_std=0.3,
            ),
            WeightedFeature(
                "interruption_count_total",
                weight=4.0,
                baseline_mean=2.0,
                baseline_std=2.0,
                direction=-1,
                recommendation="Practice pausing before replying.",
            ),
        ],
    )


def test_score_value_is_clamped_and_confidence_reports_coverage():
    result = _toy_scorer().score({"patience_sec": 0.7, "interruption_count_total": 2.0})
    assert 0.0 <= result.value <= 100.0
    assert result.confidence == 1.0  # both features provided


def test_score_value_rises_when_positive_feature_is_above_baseline():
    base = _toy_scorer().score({"patience_sec": 0.7, "interruption_count_total": 2.0})
    better = _toy_scorer().score({"patience_sec": 1.5, "interruption_count_total": 2.0})
    assert better.value > base.value


def test_score_value_falls_when_negative_feature_worsens():
    base = _toy_scorer().score({"patience_sec": 0.7, "interruption_count_total": 2.0})
    worse = _toy_scorer().score({"patience_sec": 0.7, "interruption_count_total": 10.0})
    assert worse.value < base.value


def test_top_factors_are_ranked_by_absolute_contribution():
    result = _toy_scorer().score({"patience_sec": 1.5, "interruption_count_total": 10.0})
    assert result.top_factors
    contributions = [f.magnitude_pct for f in result.top_factors]
    assert contributions == sorted(contributions, reverse=True)


def test_recommendations_only_surface_for_negative_factors():
    good = _toy_scorer().score({"patience_sec": 2.0, "interruption_count_total": 0.0})
    bad = _toy_scorer().score({"patience_sec": 0.7, "interruption_count_total": 15.0})
    assert good.recommendations == []
    assert bad.recommendations
    assert bad.recommendations[0].priority == "high"


def test_to_public_projection_does_not_leak_feature_ids():
    result = _toy_scorer().score({"patience_sec": 0.7, "interruption_count_total": 10.0})
    payload = result.to_public(expert_mode=False)
    assert "scorer_version" in payload
    for factor in payload["top_factors"]:
        assert "feature_id" not in factor  # internal only
        assert factor["direction"] in {"+", "-"}


def test_to_public_respects_expert_mode_cap():
    scorer = CompositeScorer(
        name="multi",
        version="v1",
        intercept=50.0,
        features=[
            WeightedFeature(f"feat_{i}", 1.0, 0.0, 1.0)
            for i in range(8)
        ],
    )
    inputs = {f"feat_{i}": float(i) for i in range(8)}
    result = scorer.score(inputs)
    assert len(result.to_public(expert_mode=False)["top_factors"]) <= 3
    assert len(result.to_public(expert_mode=True)["top_factors"]) <= 10


def test_missing_inputs_lower_confidence_but_do_not_crash():
    result = _toy_scorer().score({"patience_sec": 0.7})
    assert 0.0 < result.confidence < 1.0


def test_trajectory_slope_positive_for_rising_series():
    assert _trajectory_slope([1.0, 2.0, 3.0, 4.0]) > 0


def test_trajectory_slope_none_for_too_few_points():
    assert _trajectory_slope([1.0]) is None
    assert _trajectory_slope([None, None]) is None


def test_flatten_features_for_sentiment_extracts_llm_and_deterministic():
    features = {
        "deterministic": {"linguistic_style_match": 0.8, "interruption_count_total": 1},
        "llm_structured": {
            "sentiment_score": 7.0,
            "sentiment_trajectory": [{"score": 3}, {"score": 5}, {"score": 7}],
        },
    }
    flat = flatten_features_for_sentiment(features)
    assert flat["sentiment_score_llm"] == 7.0
    assert flat["sentiment_trajectory_slope"] == pytest.approx(2.0)
    assert flat["sentiment_end_valence"] == 7
    assert flat["linguistic_style_match"] == 0.8


def test_flatten_features_for_churn_counts_competitor_mentions():
    features = {
        "deterministic": {"stakeholder_count": 4},
        "llm_structured": {
            "churn_risk": 0.3,
            "competitor_mentions": [{"name": "RivalCo"}, {"name": "BasicCall"}],
            "sustain_talk_count": 2,
            "churn_risk_factors": ["freeze", "budget"],
        },
    }
    flat = flatten_features_for_churn(features)
    assert flat["competitor_pressure"] == 2
    assert flat["churn_risk_language"] == 2
    assert flat["stakeholder_count"] == 4


def test_default_scorers_construct_and_produce_a_score():
    for scorer in (default_sentiment_scorer(), default_churn_scorer(), default_health_scorer()):
        result = scorer.score({})
        assert isinstance(result, ScoreResult)
        assert 0.0 <= result.value <= 100.0
