"""Tests for the statistics library (``backend.app.services.stats``).

Each public function gets a direct test with hand-computable expected
values.  Ranges and monotonicity checks keep the tests robust to
rounding choices in the implementation.
"""

import math

import pytest

from backend.app.services.stats import (
    benjamini_hochberg,
    bootstrap_mean_ci,
    expected_calibration_error,
    fightin_words,
    krippendorff_alpha,
    platt_scale_apply,
    platt_scale_fit,
    population_stability_index,
    population_stability_index_categorical,
    two_proportion_z,
    wilson_interval,
)


def test_wilson_interval_uninformative_when_no_trials():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_narrows_as_n_grows():
    narrow = wilson_interval(50, 100)
    wider = wilson_interval(5, 10)
    assert (narrow[1] - narrow[0]) < (wider[1] - wider[0])


def test_wilson_interval_contains_observed_proportion():
    lo, hi = wilson_interval(30, 100)
    assert lo <= 0.30 <= hi


def test_two_proportion_z_returns_zero_for_equal_proportions():
    z, p = two_proportion_z(50, 100, 50, 100)
    assert abs(z) < 1e-9
    assert p == 1.0


def test_two_proportion_z_detects_real_difference():
    z, p = two_proportion_z(80, 100, 50, 100)
    assert z > 2
    assert p < 0.05


def test_bootstrap_mean_ci_returns_the_mean_and_bounds():
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]
    mean, lo, hi = bootstrap_mean_ci(samples, n_boot=500, seed=42)
    assert mean == pytest.approx(3.0)
    assert lo <= 3.0 <= hi


def test_bootstrap_mean_ci_handles_empty_and_singleton_samples():
    assert bootstrap_mean_ci([]) == (0.0, 0.0, 0.0)
    assert bootstrap_mean_ci([7.0]) == (7.0, 7.0, 7.0)


def test_fightin_words_flags_token_over_represented_in_a():
    counts_a = {"pricing": 50, "integrations": 3}
    counts_b = {"pricing": 5, "integrations": 40}
    out = fightin_words(counts_a, counts_b, alpha0=10)
    assert out["pricing"]["z"] > 2
    assert out["integrations"]["z"] < -2


def test_krippendorff_alpha_returns_one_for_perfect_agreement():
    ratings = [[4, 4, 4], [7, 7, 7], [2, 2]]
    assert krippendorff_alpha(ratings) == 1.0


def test_krippendorff_alpha_near_zero_for_random_ratings():
    ratings = [[1, 10], [10, 1], [1, 10], [10, 1]]
    alpha = krippendorff_alpha(ratings)
    assert alpha is None or alpha < 0.5


def test_population_stability_index_small_on_identical_distributions():
    expected = [float(i % 10) for i in range(1000)]
    actual = [float(i % 10) for i in range(1000)]
    assert population_stability_index(actual, expected) == pytest.approx(0.0, abs=1e-3)


def test_population_stability_index_large_on_shifted_distribution():
    expected = [float(i % 10) for i in range(1000)]
    actual = [float((i % 10) + 5) for i in range(1000)]
    assert population_stability_index(actual, expected) > 0.25


def test_population_stability_index_categorical_zero_on_match():
    counts = {"a": 10, "b": 10, "c": 10}
    psi = population_stability_index_categorical(counts, counts)
    assert psi == pytest.approx(0.0, abs=1e-6)


def test_benjamini_hochberg_rejects_tiny_p_values():
    ps = [0.001, 0.01, 0.2, 0.5, 0.9]
    rejected = benjamini_hochberg(ps, alpha=0.05)
    assert rejected[0] is True
    assert rejected[-1] is False


def test_platt_scale_fit_and_apply_recover_separable_classes():
    raw = [-3.0, -2.0, -1.0, 1.0, 2.0, 3.0]
    y = [0, 0, 0, 1, 1, 1]
    A, B = platt_scale_fit(raw, y, lr=0.3, n_iter=3000)
    assert platt_scale_apply(3.0, A, B) > platt_scale_apply(-3.0, A, B)
    # Both probabilities in (0, 1).
    for s in raw:
        p = platt_scale_apply(s, A, B)
        assert 0.0 < p < 1.0


def test_expected_calibration_error_small_for_good_calibration():
    probs = [0.1] * 100 + [0.9] * 100
    outcomes = [0] * 90 + [1] * 10 + [1] * 90 + [0] * 10
    ece = expected_calibration_error(probs, outcomes)
    # Expected: perfect calibration ⇒ ECE ≈ 0 for these buckets.
    assert ece < 0.05
