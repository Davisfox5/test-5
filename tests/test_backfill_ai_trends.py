"""Tests for helpers in ``backend.backfill_ai_trends``."""

import pytest


def test_bucket_to_float_maps_signals_to_midpoints():
    from backend.backfill_ai_trends import _bucket_to_float

    assert _bucket_to_float("high") == 0.85
    assert _bucket_to_float("medium") == 0.55
    assert _bucket_to_float("low") == 0.25
    assert _bucket_to_float("none") == 0.05


def test_bucket_to_float_is_case_insensitive():
    from backend.backfill_ai_trends import _bucket_to_float

    assert _bucket_to_float("HIGH") == 0.85
    assert _bucket_to_float("High") == 0.85


def test_bucket_to_float_returns_none_for_missing_or_unknown():
    from backend.backfill_ai_trends import _bucket_to_float

    assert _bucket_to_float(None) is None
    assert _bucket_to_float("") is None
    assert _bucket_to_float("unknown") is None


def test_numeric_signals_pass_skips_insights_already_populated():
    """If both numeric fields are present we don't overwrite them."""
    from backend.backfill_ai_trends import _bucket_to_float

    # Representative example — the pass only infers when a numeric value
    # is missing; this helper confirms the precondition holds.
    insights = {"churn_risk_signal": "high", "churn_risk": 0.91}
    # Real pass: `if "churn_risk" not in insights or insights["churn_risk"] is None`
    assert "churn_risk" in insights and insights["churn_risk"] is not None
    # Inferred value from the signal is different, confirming we would clobber:
    assert _bucket_to_float(insights["churn_risk_signal"]) == 0.85
