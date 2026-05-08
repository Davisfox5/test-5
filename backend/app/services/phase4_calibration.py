"""Calibration metrics for Phase 4 binary classifiers.

Pure-Python implementations of Brier score, Expected Calibration Error
(ECE), and reliability-diagram bins. Mirrors what scikit-learn's
``calibration_curve`` produces but without the dep — same precedent as
the rest of the Phase 4 codebase.

* **Brier score** — mean squared error between predicted probability
  and binary outcome. 0 = perfect, 0.25 = random for balanced labels.
  Lower is better.

* **Expected Calibration Error (ECE)** — weighted absolute difference
  between predicted probability and empirical positive rate within
  equal-width bins. 0 = perfectly calibrated regardless of accuracy.

* **Reliability diagram bins** — per-bin (mean prediction, empirical
  positive rate, count) so a UI can render the calibration curve.

These three together give us:

1. Are predictions sharp? (Brier captures both calibration + sharpness)
2. Are predictions calibrated? (ECE isolates calibration from sharpness)
3. Where does calibration break? (reliability bins show *which*
   predicted-probability ranges are over- or under-confident)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence


def brier_score(predictions: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between predicted probabilities and binary outcomes.

    Returns 0.0 on an empty input rather than raising — keeps the
    metrics dict serialisable from the training task even on edge
    cases. Values outside [0, 1] are clipped to that range so a buggy
    classifier doesn't poison the score.
    """
    if not predictions:
        return 0.0
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"predictions ({len(predictions)}) and outcomes "
            f"({len(outcomes)}) must have the same length"
        )
    total = 0.0
    for p, y in zip(predictions, outcomes):
        p_clipped = max(0.0, min(1.0, float(p)))
        total += (p_clipped - float(y)) ** 2
    return total / len(predictions)


@dataclass
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_prediction: float
    empirical_rate: float


def reliability_bins(
    predictions: Sequence[float],
    outcomes: Sequence[int],
    n_bins: int = 10,
) -> List[ReliabilityBin]:
    """Equal-width bins over [0, 1].

    Bin assignment: ``i = floor(p * n_bins)`` (clamped to ``n_bins-1``).
    Empty bins emit ``count=0`` with mean_prediction = empirical_rate
    set to the bin midpoint so a UI rendering can still show every bin
    on the x-axis without holes.
    """
    if n_bins < 1:
        raise ValueError("n_bins must be at least 1")
    if len(predictions) != len(outcomes):
        raise ValueError("predictions / outcomes length mismatch")
    width = 1.0 / n_bins
    bins: List[List[tuple]] = [[] for _ in range(n_bins)]
    for p, y in zip(predictions, outcomes):
        p_clipped = max(0.0, min(1.0, float(p)))
        idx = min(n_bins - 1, int(p_clipped * n_bins))
        bins[idx].append((p_clipped, int(y)))
    out: List[ReliabilityBin] = []
    for i, items in enumerate(bins):
        lower = i * width
        upper = (i + 1) * width
        if not items:
            mid = (lower + upper) / 2
            out.append(
                ReliabilityBin(
                    lower=lower,
                    upper=upper,
                    count=0,
                    mean_prediction=mid,
                    empirical_rate=mid,
                )
            )
            continue
        mean_p = sum(p for p, _ in items) / len(items)
        empirical = sum(y for _, y in items) / len(items)
        out.append(
            ReliabilityBin(
                lower=lower,
                upper=upper,
                count=len(items),
                mean_prediction=mean_p,
                empirical_rate=empirical,
            )
        )
    return out


def expected_calibration_error(
    predictions: Sequence[float],
    outcomes: Sequence[int],
    n_bins: int = 10,
) -> float:
    """Equal-width-bin ECE.

    Definition: ``ECE = Σ_b (|B_b| / n) · |mean_pred_b − empirical_b|``.
    A perfectly calibrated classifier has ECE = 0; a classifier that
    always says 0.5 on a 10% positive-rate dataset has ECE = 0.4.
    """
    if not predictions:
        return 0.0
    bins = reliability_bins(predictions, outcomes, n_bins=n_bins)
    n = len(predictions)
    total = 0.0
    for b in bins:
        if b.count == 0:
            continue
        weight = b.count / n
        total += weight * abs(b.mean_prediction - b.empirical_rate)
    return total
