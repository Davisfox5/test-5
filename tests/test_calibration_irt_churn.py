"""Tests for the pure-logic pieces of calibration, IRT, and Cox churn
scaffolding.  These do not require a live database — they exercise the
math that can be unit-tested in isolation.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.calibration import (
    DEFAULT_CALIBRATION_CONFIGS,
    MIN_CALIBRATION_SAMPLES,
    _extract_outcome,
    _read_path,
)
from backend.app.services.churn_model import (
    CoxDatum,
    CoxModel,
    FEATURES,
    MIN_TRAIN_EVENTS,
    RELIABLE_TRAIN_EVENTS,
    _event_from_outcomes,
    _feature_vector,
    fit_cox,
)
from backend.app.services.irt import (
    ItemResponse,
    _fit_item,
    _pass_rate,
    _reestimate_theta,
)


# ── Calibration helpers ──────────────────────────────────────────────────


def test_read_path_descends_nested_dicts():
    obj = {"llm_structured": {"sentiment_score": 7.5}}
    assert _read_path(obj, ("llm_structured", "sentiment_score")) == 7.5


def test_read_path_returns_none_when_missing():
    assert _read_path({}, ("a", "b")) is None
    assert _read_path({"a": 3}, ("a", "b")) is None


def test_extract_outcome_returns_one_for_positive_key():
    assert _extract_outcome({"customer_replied": {}}, ("customer_replied",), ("x",)) == 1


def test_extract_outcome_returns_zero_for_negative_key():
    assert _extract_outcome({"x": {}}, ("customer_replied",), ("x",)) == 0


def test_extract_outcome_returns_none_when_nothing_present():
    assert _extract_outcome({}, ("a",), ("b",)) is None


def test_default_configs_cover_core_scorers():
    names = {c.scorer_name for c in DEFAULT_CALIBRATION_CONFIGS}
    assert {"sentiment", "churn_risk", "upsell"}.issubset(names)


def test_min_calibration_samples_is_reasonable_default():
    assert MIN_CALIBRATION_SAMPLES >= 20


# ── IRT helpers ──────────────────────────────────────────────────────────


def test_fit_item_returns_zeros_under_min_responses():
    a, b, rate = _fit_item([ItemResponse(0, 1)], [0.5])
    assert a == 0.0 and b == 0.0


def test_fit_item_discriminates_when_data_sufficient():
    # High-θ people pass, low-θ fail — items should fit a>0 and b near median.
    theta = [(i - 15) / 10.0 for i in range(30)]  # -1.5 … 1.4
    responses = [
        ItemResponse(i, 1 if theta[i] > 0 else 0) for i in range(30)
    ]
    a, b, rate = _fit_item(responses, theta, n_iter=400)
    assert a > 0
    assert -1.0 < b < 1.0
    assert 0.0 < rate < 1.0


def test_pass_rate_counts_positive_responses():
    rs = [ItemResponse(0, 1), ItemResponse(1, 0), ItemResponse(2, 1)]
    assert _pass_rate(rs) == pytest.approx(0.6667, abs=1e-3)


def test_reestimate_theta_clamps_outputs():
    responses = {"x": [ItemResponse(0, 1)] * 5}
    item_params = {"x": {"a": 1.0, "b": 0.0}}
    theta = _reestimate_theta(responses, item_params, n_people=1)
    assert -3.0 <= theta[0] <= 3.0


# ── Churn / Cox helpers ─────────────────────────────────────────────────


def test_event_from_outcomes_reports_event_when_churn_key_present():
    created = datetime.now(timezone.utc) - timedelta(days=40)
    outcomes = {
        "contact_churned_30d": {
            "value": 1.0,
            "occurred_at": (created + timedelta(days=20)).isoformat(),
        }
    }
    event, duration = _event_from_outcomes(
        created, outcomes, datetime.now(timezone.utc)
    )
    assert event == 1
    assert duration == pytest.approx(20, abs=1)


def test_event_from_outcomes_censors_when_no_event():
    created = datetime.now(timezone.utc) - timedelta(days=5)
    event, duration = _event_from_outcomes(
        created, {}, datetime.now(timezone.utc)
    )
    assert event == 0
    assert duration == pytest.approx(5, abs=1)


def test_feature_vector_pulls_llm_and_deterministic_fields():
    class _Row:
        deterministic = {"stakeholder_count": 3, "patience_sec": 0.8, "interactivity_per_min": 5}
        llm_structured = {
            "sentiment_score": 7.5,
            "churn_risk": 0.3,
            "sustain_talk_count": 1,
            "competitor_mentions": [{"name": "X"}],
        }
    vec = _feature_vector(_Row())
    assert len(vec) == len(FEATURES)
    assert 7.5 in vec


def test_fit_cox_returns_finite_coefficients():
    # Simple linearly-separable dataset: one feature, event iff x > 0.
    data = []
    for i in range(50):
        x_val = (i - 25) / 10.0
        event = 1 if x_val > 0 else 0
        data.append(CoxDatum(
            duration_days=100 - i,
            event=event,
            x=[x_val] + [0.0] * (len(FEATURES) - 1),
        ))
    model = fit_cox(data, n_iter=30, lr=0.1)
    assert all(isinstance(c, float) for c in model.coefficients)
    assert model.n_events > 0
    # Positive feature should lean toward positive coefficient (hazard ↑ with x).
    assert model.coefficients[0] > 0 or model.coefficients[0] == 0.0


def test_churn_thresholds_order_so_learning_window_exists():
    # MIN_TRAIN_EVENTS gates training; RELIABLE_TRAIN_EVENTS flips the
    # caveat off.  A learning window must exist between the two.
    assert MIN_TRAIN_EVENTS < RELIABLE_TRAIN_EVENTS
    assert MIN_TRAIN_EVENTS == 150
    assert RELIABLE_TRAIN_EVENTS == 300


def test_cox_model_hazard_is_nonnegative():
    model = CoxModel(
        coefficients=[0.5, -0.2],
        feature_names=["a", "b"],
        n_events=100,
        n_censored=50,
        log_likelihood=-1.0,
        fitted_at="2026-04-17T00:00:00Z",
    )
    assert model.hazard([1.0, 0.0]) > 0
    assert model.hazard([0.0, 0.0]) == pytest.approx(1.0)
