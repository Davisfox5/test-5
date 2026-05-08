"""Tests for Phase 4 calibration metrics.

Pinning the Brier / ECE math + the reliability-bin layout so a
stealth tweak gets caught at PR time. These metrics drive the
manager-dashboard calibration UI; if the numbers shift unexpectedly
the chart gets misleading.
"""

from __future__ import annotations

import pytest

from backend.app.services.phase4_calibration import (
    ReliabilityBin,
    brier_score,
    expected_calibration_error,
    reliability_bins,
)


def test_brier_score_perfect_predictions():
    """All correct → Brier = 0."""
    assert brier_score([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0]) == 0.0


def test_brier_score_worst_predictions():
    """All wrong with full confidence → Brier = 1."""
    assert brier_score([0.0, 1.0, 0.0, 1.0], [1, 0, 1, 0]) == 1.0


def test_brier_score_random_constant_predictor():
    """Always 0.5 against balanced labels → 0.25."""
    assert brier_score([0.5] * 6, [1, 0, 1, 0, 1, 0]) == 0.25


def test_brier_score_clips_out_of_range_input():
    """Out-of-range predictions get clipped, not exception'd — keeps the
    metrics dict serialisable when a bug ships into production."""
    bs = brier_score([1.5, -0.3, 1.0], [1, 0, 1])
    # 1.5 → 1.0 (correct on label 1) → 0
    # -0.3 → 0.0 (correct on label 0) → 0
    # 1.0 (correct on label 1) → 0
    assert bs == 0.0


def test_brier_score_empty_returns_zero():
    """Empty input doesn't blow up the training task — the trainer
    might call this on a degenerate split."""
    assert brier_score([], []) == 0.0


def test_brier_score_length_mismatch_raises():
    with pytest.raises(ValueError):
        brier_score([0.5, 0.5], [1, 0, 1])


def test_reliability_bins_default_n_bins():
    """10 bins by default."""
    bins = reliability_bins([0.05, 0.95], [0, 1])
    assert len(bins) == 10


def test_reliability_bins_assignment():
    """``floor(p * n_bins)`` clamped to ``n_bins-1`` — the right edge
    must land in the last bin, not overflow."""
    bins = reliability_bins([0.0, 0.99, 1.0, 0.5], [0, 1, 1, 1], n_bins=10)
    # 0.0 → bin 0; 0.99 → bin 9; 1.0 → bin 9 (clamp); 0.5 → bin 5.
    assert bins[0].count == 1
    assert bins[5].count == 1
    assert bins[9].count == 2


def test_reliability_bins_empty_bin_renders_midpoint():
    """Empty bins still render — keeps the curve continuous on the UI."""
    bins = reliability_bins([0.05, 0.95], [0, 1], n_bins=10)
    # Bins 1-8 are empty; midpoint placeholder so the chart has every x.
    middle = bins[4]
    assert middle.count == 0
    assert middle.mean_prediction == pytest.approx(0.45, abs=1e-9)
    assert middle.empirical_rate == pytest.approx(0.45, abs=1e-9)


def test_ece_perfectly_calibrated():
    """Predicting at the empirical rate within each bin → ECE 0."""
    preds = [0.1] * 10 + [0.9] * 10
    outcomes = [0] * 9 + [1] + [1] * 9 + [0]
    # bin 1 (0.1-0.2): 10 preds at 0.1, empirical = 1/10 = 0.1 → match
    # bin 9 (0.9-1.0): 10 preds at 0.9, empirical = 9/10 = 0.9 → match
    ece = expected_calibration_error(preds, outcomes)
    assert ece == pytest.approx(0.0, abs=1e-9)


def test_ece_constant_overconfident():
    """Always 0.5 against a 10%-positive dataset → ECE = 0.4."""
    preds = [0.5] * 100
    outcomes = [1] * 10 + [0] * 90
    ece = expected_calibration_error(preds, outcomes)
    assert ece == pytest.approx(0.4, abs=1e-9)


def test_ece_handles_n_bins_one():
    """One bin reduces to ``|mean_pred − mean_actual|``."""
    preds = [0.6, 0.7, 0.8]
    outcomes = [1, 0, 1]
    ece = expected_calibration_error(preds, outcomes, n_bins=1)
    # mean_pred = 0.7, empirical = 2/3 ≈ 0.667 → ECE ≈ 0.033
    assert ece == pytest.approx(abs(0.7 - 2 / 3), abs=1e-9)


def test_ece_empty_returns_zero():
    assert expected_calibration_error([], []) == 0.0


def test_reliability_bin_dataclass_shape():
    """Pin the field names so a UI consumer can rely on them."""
    bin_ = ReliabilityBin(
        lower=0.0, upper=0.1, count=3, mean_prediction=0.05, empirical_rate=0.33,
    )
    assert bin_.lower == 0.0
    assert bin_.upper == 0.1
    assert bin_.count == 3
    assert bin_.mean_prediction == 0.05
    assert bin_.empirical_rate == 0.33
