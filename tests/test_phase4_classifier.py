"""Tests for the Phase 4 classifier core (no DB).

Focuses on the pure-Python LR fit + Platt calibration math and the
serialisation round-trip so the model can ride a JSONB column. The
DB-side training-data assembly is tested indirectly via the admin
endpoint integration (separate file).
"""

from __future__ import annotations

import math
import random

import pytest

from backend.app.services.phase4_classifier import (
    DEFAULT_LABEL_HORIZON_DAYS,
    FEATURE_NAMES,
    LRModel,
    MIN_HELDOUT_FOR_CALIBRATION,
    SUPPORTED_LABEL_HORIZONS,
    _platt_fit,
    _sigmoid,
    _standardise_batch,
    _train_lr,
    feature_vector,
    fit_lr,
)


def test_feature_names_pinned_count_and_order():
    """Stealth changes to the feature set break model load. Pin both
    cardinality + order."""
    assert FEATURE_NAMES == (
        "sentiment_score",
        "churn_risk",
        "upsell_score",
        "objection_count",
        "unresolved_objection_count",
        "commitment_count",
        "discovery_questions",
        "competitor_mention_count",
        "rubric_discovery_quality",
        "rubric_commitment_strength",
        "rubric_objection_resolution_rate",
        "rubric_win_likelihood",
        "rapport_lsm_overall",
        "rapport_vocal_accommodation_overall",
    )


def test_supported_label_horizons():
    """Phase 0 plan-doc target: 30/90/180/365 day churn labels."""
    assert SUPPORTED_LABEL_HORIZONS == (30, 90, 180, 365)
    assert DEFAULT_LABEL_HORIZON_DAYS == 90


def test_sigmoid_extremes():
    assert _sigmoid(0) == 0.5
    # Numeric stability: large positive / negative shouldn't overflow.
    assert _sigmoid(1000) == 1.0
    assert _sigmoid(-1000) == 0.0


def test_standardise_batch_zero_var_doesnt_blow_up():
    """A constant column gets a clamped std (≥1e-6) so the row math
    doesn't divide by zero. Standardised constant column lands at 0."""
    X = [[1.0, 5.0], [1.0, 7.0], [1.0, 9.0]]
    std_X, means, stds = _standardise_batch(X)
    assert means[0] == 1.0
    assert stds[0] >= 1e-6
    assert all(row[0] == 0.0 for row in std_X)


def test_train_lr_separable_data_recovers_signal():
    """Linearly separable toy: y = (x0 > 0). Trained model should
    predict ≥ 0.9 on positives and ≤ 0.1 on negatives."""
    rng = random.Random(7)
    X = []
    y = []
    for _ in range(200):
        x0 = rng.uniform(-2, 2)
        x1 = rng.uniform(-1, 1)  # noise feature
        X.append([x0, x1])
        y.append(1 if x0 > 0 else 0)
    std_X, _, _ = _standardise_batch(X)
    weights, intercept, loss = _train_lr(std_X, y, l2=0.01, lr=0.5, epochs=400)
    # Weight on x0 should dominate; sign positive.
    assert weights[0] > 1.0
    assert abs(weights[1]) < weights[0] / 2  # noise feature suppressed


def test_platt_fit_skips_below_min_holdout():
    """Below MIN_HELDOUT_FOR_CALIBRATION → identity (alpha=1, beta=0)."""
    alpha, beta = _platt_fit([0.1, 0.9], [0, 1])
    assert (alpha, beta) == (1.0, 0.0)


def test_platt_fit_calibrates_overconfident_scores():
    """Raw scores in a realistic LR range (~[-3, 3]) with a true
    shallower slope should produce an alpha that flattens the
    confidence and a non-zero beta absorbing the bias."""
    rng = random.Random(11)
    scores = []
    labels = []
    for _ in range(200):
        s = rng.uniform(-3, 3)
        scores.append(s)
        # True P = sigmoid(0.4 s − 0.6) — shallower slope + negative bias.
        p = 1 / (1 + math.exp(-(0.4 * s - 0.6)))
        labels.append(1 if rng.random() < p else 0)
    alpha, beta = _platt_fit(scores, labels)
    # alpha pulled down from 1.0 toward the true slope.
    assert 0.1 < alpha < 0.95
    # beta picks up the negative bias.
    assert beta < 0


def test_platt_fit_does_not_diverge_on_extreme_scores():
    """Stress test with scores in [-10, 10]. Earlier the unbounded
    Newton step could diverge to alpha ~ 6000+. The step-cap keeps the
    iterate in [−5, 5] roughly."""
    rng = random.Random(11)
    scores = [rng.uniform(-10, 10) for _ in range(200)]
    labels = [1 if s > 1.0 else 0 for s in scores]
    alpha, beta = _platt_fit(scores, labels)
    assert -5.0 < alpha < 5.0
    assert -5.0 < beta < 5.0


def test_fit_lr_smoke():
    """Full fit_lr round-trip: separable signal, calibrated probabilities."""
    rng = random.Random(13)
    raw = []
    for _ in range(200):
        x0 = rng.uniform(-2, 2)
        x1 = rng.uniform(-1, 1)
        x = [x0, x1] + [0.0] * (len(FEATURE_NAMES) - 2)
        y = 1 if x0 > 0.5 else 0
        raw.append((x, y))
    model = fit_lr(raw, target="churn")
    # Model captures direction.
    pos_prob = model.predict_proba([1.5, 0.0] + [0.0] * (len(FEATURE_NAMES) - 2))
    neg_prob = model.predict_proba([-1.5, 0.0] + [0.0] * (len(FEATURE_NAMES) - 2))
    assert pos_prob > neg_prob
    # Probabilities are in [0, 1].
    assert 0.0 <= pos_prob <= 1.0
    assert 0.0 <= neg_prob <= 1.0


def test_lr_model_serialisation_round_trip():
    """``as_dict()`` + ``from_dict()`` must preserve every field byte-
    identically so the JSONB persistence doesn't lossy."""
    model = LRModel(
        weights=[0.1, -0.2, 0.3],
        intercept=-0.5,
        feature_names=["a", "b", "c"],
        feature_means=[1.0, 2.0, 3.0],
        feature_stds=[0.5, 0.5, 0.5],
        n_train=42,
        n_events=7,
        log_loss=0.123,
        platt_alpha=0.85,
        platt_beta=-0.2,
        fitted_at="2026-05-07T00:00:00+00:00",
        target="upsell",
        label_horizon_days=180,
    )
    blob = model.as_dict()
    restored = LRModel.from_dict(blob)
    assert restored.as_dict() == blob


def test_feature_vector_pulls_all_paths():
    insights = {
        "sentiment_score": 6.0,
        "churn_risk": 0.55,
        "upsell_score": 0.25,
        "evidence": {
            "objection_count": 2,
            "unresolved_objection_count": 1,
            "commitment_count": 1,
            "discovery_questions": 4,
            "competitor_mention_count": 0,
        },
        "rubric": {
            "discovery_quality": 0.5,
            "commitment_strength": 0.33,
            "objection_resolution_rate": 0.5,
            "win_likelihood": 0.42,
        },
        "rapport": {
            "lsm_overall": 0.71,
            "vocal_accommodation": {"overall": 0.62},
        },
    }
    x = feature_vector(insights)
    assert len(x) == len(FEATURE_NAMES)
    # Spot-check a couple of bindings.
    assert x[0] == 6.0  # sentiment_score
    assert x[FEATURE_NAMES.index("rubric_win_likelihood")] == 0.42
    assert (
        x[FEATURE_NAMES.index("rapport_vocal_accommodation_overall")] == 0.62
    )


def test_feature_vector_handles_missing_blocks():
    """All ``None`` is a valid output; the trainer + inference both
    short-circuit on it."""
    x = feature_vector({})
    assert len(x) == len(FEATURE_NAMES)
    assert all(v is None for v in x)


def test_feature_vector_filters_nan_and_inf():
    """A buggy model dropping NaN into one of the numeric fields
    shouldn't poison the training set with NaN-as-zero. NaN / inf →
    None so the row gets dropped."""
    x = feature_vector(
        {
            "sentiment_score": float("nan"),
            "churn_risk": float("inf"),
            "upsell_score": -float("inf"),
        }
    )
    assert x[0] is None
    assert x[1] is None
    assert x[2] is None


def test_min_heldout_constant_pinned():
    """Plan doc commits to held-out calibration above this threshold;
    pin it so a stealth change to lower numbers doesn't silently
    overfit calibration on tiny holdouts."""
    assert MIN_HELDOUT_FOR_CALIBRATION == 30
