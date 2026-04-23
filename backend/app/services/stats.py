"""Statistical primitives used across the scoring, orchestrator, and
analytics layers.

Pure-Python (no numpy/scipy dependency) so these functions can run inside
Celery workers, FastAPI handlers, and tests without extra install steps.
All routines are side-effect free and return plain dict/float/tuple values
suitable for JSONB storage.

Implemented:

- :func:`wilson_interval` — Wilson score CI for a proportion.
- :func:`bootstrap_mean_ci` — BCa-style bootstrap CI for a sample mean.
- :func:`fightin_words` — Monroe/Colaresi/Quinn log-odds with an informative
  Dirichlet prior; robust topic-trend significance vs. naive pct_change.
- :func:`two_proportion_z` — z-test for comparing two proportions.
- :func:`krippendorff_alpha` — inter-rater reliability on an ordinal or
  interval scale; works for any number of raters and missing values.
- :func:`population_stability_index` — PSI for monitoring distribution
  drift of a continuous or categorical feature.
- :func:`benjamini_hochberg` — BH-FDR multiple-testing correction.
- :func:`platt_scale_fit` / :func:`platt_scale_apply` — calibration of a
  raw scorer output to a probability via logistic regression on
  (raw_score, observed_outcome).
- :func:`expected_calibration_error` — ECE for a calibrated classifier.

References are inline.  Formulas are chosen to be numerically stable on
small samples.
"""

from __future__ import annotations

import math
import random
from statistics import mean
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# ── Proportions ───────────────────────────────────────────────────────────


def wilson_interval(
    successes: int,
    trials: int,
    confidence: float = 0.95,
) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Prefer this over the normal-approximation interval when ``trials`` is
    small or ``p`` is near 0/1 — Brown, Cai & DasGupta (2001).  Returns
    ``(lower, upper)`` bounds in ``[0, 1]``.  When ``trials == 0`` returns
    ``(0.0, 1.0)`` — the uninformative interval.
    """
    if trials <= 0:
        return (0.0, 1.0)
    z = _z_for_confidence(confidence)
    p = successes / trials
    denom = 1.0 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    half = (z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def two_proportion_z(
    succ_a: int, n_a: int,
    succ_b: int, n_b: int,
) -> Tuple[float, float]:
    """Two-proportion z-test.  Returns ``(z, two_sided_p_value)``.

    Null: ``p_a == p_b``.  Pooled-variance form.
    """
    if n_a <= 0 or n_b <= 0:
        return (0.0, 1.0)
    p_a = succ_a / n_a
    p_b = succ_b / n_b
    p_pool = (succ_a + succ_b) / (n_a + n_b)
    var = p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b)
    if var <= 0:
        return (0.0, 1.0)
    z = (p_a - p_b) / math.sqrt(var)
    p = 2 * (1 - _normal_cdf(abs(z)))
    return (z, p)


# ── Means ─────────────────────────────────────────────────────────────────


def bootstrap_mean_ci(
    samples: Sequence[float],
    confidence: float = 0.95,
    n_boot: int = 2000,
    seed: Optional[int] = None,
) -> Tuple[float, float, float]:
    """Percentile bootstrap CI for the mean of ``samples``.

    Returns ``(mean, lower, upper)`` at the requested confidence level.
    Deterministic given ``seed``.  Fast enough for interactive use with
    n up to ~10k samples and n_boot=2000.
    """
    samples = [float(x) for x in samples if x is not None]
    n = len(samples)
    if n == 0:
        return (0.0, 0.0, 0.0)
    if n == 1:
        return (samples[0], samples[0], samples[0])

    rng = random.Random(seed)
    means: List[float] = []
    for _ in range(n_boot):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    alpha = (1 - confidence) / 2
    lo = means[int(alpha * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha) * n_boot))]
    return (sum(samples) / n, lo, hi)


# ── Topic trends (Fightin' Words) ─────────────────────────────────────────


def fightin_words(
    counts_a: Dict[str, int],
    counts_b: Dict[str, int],
    prior: Optional[Dict[str, float]] = None,
    alpha0: float = 100.0,
) -> Dict[str, Dict[str, float]]:
    """Log-odds-ratio with informative Dirichlet prior.

    Monroe, Colaresi & Quinn (2008), *Fightin' Words*.  For each token
    present in either corpus, return ``{token: {delta, se, z}}``:

    - ``delta`` = log-odds difference (positive ⇒ over-represented in A)
    - ``se`` = approximate standard error
    - ``z`` = ``delta / se`` — drop-in replacement for naive pct-change

    ``prior`` is an optional Dirichlet pseudocount per token; if omitted
    a uniform prior with total mass ``alpha0`` is used.
    """
    vocab = set(counts_a) | set(counts_b)
    if prior is None:
        alpha_w = alpha0 / max(len(vocab), 1)
        prior = {w: alpha_w for w in vocab}

    alpha_total = sum(prior.values())
    n_a = sum(counts_a.values())
    n_b = sum(counts_b.values())

    out: Dict[str, Dict[str, float]] = {}
    for w in vocab:
        y_a = counts_a.get(w, 0)
        y_b = counts_b.get(w, 0)
        a_w = prior.get(w, 0.0)

        num_a = y_a + a_w
        den_a = n_a + alpha_total - y_a - a_w
        num_b = y_b + a_w
        den_b = n_b + alpha_total - y_b - a_w
        if num_a <= 0 or den_a <= 0 or num_b <= 0 or den_b <= 0:
            continue

        delta = math.log(num_a / den_a) - math.log(num_b / den_b)
        var = 1.0 / num_a + 1.0 / num_b
        se = math.sqrt(var)
        out[w] = {
            "delta": round(delta, 4),
            "se": round(se, 4),
            "z": round(delta / se, 4) if se > 0 else 0.0,
        }
    return out


# ── Inter-rater reliability ───────────────────────────────────────────────


def krippendorff_alpha(
    ratings: Sequence[Sequence[Optional[float]]],
    level: str = "interval",
) -> Optional[float]:
    """Krippendorff's α for reliability across ``k`` raters on ``n`` items.

    ``ratings`` is shaped ``[n_items][n_raters]`` with ``None`` marking
    missing values.  Implements nominal, ordinal, and interval metrics.
    Returns ``None`` when too few comparable pairs exist.

    Guide (Krippendorff, 2004): α ≥ 0.80 = acceptable; 0.67–0.80 =
    tentative; below 0.67 = unreliable.
    """
    # Collect paired observations per item.
    units: List[List[float]] = []
    for row in ratings:
        vals = [float(v) for v in row if v is not None]
        if len(vals) >= 2:
            units.append(vals)
    if not units:
        return None

    # Flatten to "coincidence" pairs.
    def _dist(x: float, y: float) -> float:
        if level == "nominal":
            return 0.0 if x == y else 1.0
        if level == "ordinal":
            return (x - y) ** 2
        return (x - y) ** 2  # interval

    observed_num = 0.0
    pair_count = 0
    all_values: List[float] = []
    for vals in units:
        all_values.extend(vals)
        m = len(vals)
        for i in range(m):
            for j in range(m):
                if i != j:
                    observed_num += _dist(vals[i], vals[j])
                    pair_count += 1
    if pair_count == 0 or len(all_values) < 2:
        return None
    observed = observed_num / pair_count

    n_total = len(all_values)
    expected_num = 0.0
    for i, x in enumerate(all_values):
        for j, y in enumerate(all_values):
            if i != j:
                expected_num += _dist(x, y)
    expected_denom = n_total * (n_total - 1)
    if expected_denom == 0:
        return None
    expected = expected_num / expected_denom
    if expected == 0:
        return 1.0 if observed == 0 else None
    return round(1.0 - observed / expected, 4)


# ── Distribution drift ────────────────────────────────────────────────────


def population_stability_index(
    actual: Sequence[float],
    expected: Sequence[float],
    n_bins: int = 10,
) -> float:
    """Population Stability Index between two numeric distributions.

    Interpretation (industry standard, credit-scoring literature):
      * PSI < 0.10 → insignificant change
      * 0.10 ≤ PSI < 0.25 → moderate shift, investigate
      * PSI ≥ 0.25 → significant shift, recalibrate
    """
    actual = [float(x) for x in actual if x is not None]
    expected = [float(x) for x in expected if x is not None]
    if not actual or not expected:
        return 0.0

    # Build bin edges from the expected distribution (quantiles).
    expected_sorted = sorted(expected)
    edges = [
        expected_sorted[min(len(expected_sorted) - 1, int(i * len(expected_sorted) / n_bins))]
        for i in range(1, n_bins)
    ]

    def _bin(x: float) -> int:
        for i, edge in enumerate(edges):
            if x < edge:
                return i
        return n_bins - 1

    act_counts = [0] * n_bins
    exp_counts = [0] * n_bins
    for x in actual:
        act_counts[_bin(x)] += 1
    for x in expected:
        exp_counts[_bin(x)] += 1

    eps = 1e-6
    psi = 0.0
    for a, e in zip(act_counts, exp_counts):
        a_pct = a / len(actual) + eps
        e_pct = e / len(expected) + eps
        psi += (a_pct - e_pct) * math.log(a_pct / e_pct)
    return round(psi, 4)


def population_stability_index_categorical(
    actual: Dict[str, int],
    expected: Dict[str, int],
) -> float:
    """PSI across categorical buckets (same interpretation thresholds)."""
    categories = set(actual) | set(expected)
    a_total = max(sum(actual.values()), 1)
    e_total = max(sum(expected.values()), 1)
    eps = 1e-6
    psi = 0.0
    for c in categories:
        a_pct = actual.get(c, 0) / a_total + eps
        e_pct = expected.get(c, 0) / e_total + eps
        psi += (a_pct - e_pct) * math.log(a_pct / e_pct)
    return round(psi, 4)


# ── Multiple testing ──────────────────────────────────────────────────────


def benjamini_hochberg(
    p_values: Sequence[float],
    alpha: float = 0.05,
) -> List[bool]:
    """Benjamini-Hochberg false discovery rate control.

    Returns a list of booleans aligned with ``p_values`` — True where the
    corresponding hypothesis is rejected at FDR ≤ ``alpha``.
    """
    n = len(p_values)
    if n == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda t: t[1])
    rejected = [False] * n
    threshold = 0
    for rank, (_, p) in enumerate(indexed, start=1):
        if p <= (rank / n) * alpha:
            threshold = rank
    for rank, (idx, _) in enumerate(indexed, start=1):
        if rank <= threshold:
            rejected[idx] = True
    return rejected


# ── Calibration ───────────────────────────────────────────────────────────


def platt_scale_fit(
    raw_scores: Sequence[float],
    outcomes: Sequence[int],
    lr: float = 0.05,
    n_iter: int = 500,
) -> Tuple[float, float]:
    """Fit Platt scaling ``P(y=1|s) = σ(A*s + B)`` where σ is the logistic.

    Uses batch gradient descent on the cross-entropy loss — fine for the
    O(10^3) points we expect per tenant.  Returns ``(A, B)``.  A is
    *positive* when higher raw scores correlate with positive outcomes,
    matching the intuitive sign.
    """
    pairs = [(float(s), int(y)) for s, y in zip(raw_scores, outcomes) if s is not None]
    if not pairs:
        return (0.0, 0.0)

    # Platt's trick: smooth labels to avoid log(0).
    n_pos = sum(y for _, y in pairs)
    n_neg = len(pairs) - n_pos
    t_pos = (n_pos + 1) / (n_pos + 2) if n_pos else 0.5
    t_neg = 1 / (n_neg + 2) if n_neg else 0.5

    # Minimize L = -Σ t log p + (1-t) log(1-p) where p = σ(z), z = A*s + B.
    # dL/dz = p − t, so dL/dA = (p − t)*s and dL/dB = (p − t).
    A, B = 0.0, 0.0
    for _ in range(n_iter):
        gA = gB = 0.0
        for s, y in pairs:
            t = t_pos if y == 1 else t_neg
            z = max(-35.0, min(35.0, A * s + B))
            p = 1.0 / (1.0 + math.exp(-z))
            err = p - t  # dL/dz
            gA += err * s
            gB += err
        gA /= len(pairs)
        gB /= len(pairs)
        A -= lr * gA
        B -= lr * gB
    return (A, B)


def platt_scale_apply(raw_score: float, A: float, B: float) -> float:
    """Apply a fitted Platt scale to a new raw score (standard logistic)."""
    z = max(-35.0, min(35.0, A * raw_score + B))
    return 1.0 / (1.0 + math.exp(-z))


def expected_calibration_error(
    probs: Sequence[float],
    outcomes: Sequence[int],
    n_bins: int = 10,
) -> float:
    """Expected Calibration Error (Guo et al., 2017).

    Lower is better.  ECE > 0.12 is a typical recalibration trigger.
    """
    if not probs:
        return 0.0
    bins: List[List[Tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in zip(probs, outcomes):
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx].append((p, int(y)))
    total = len(probs)
    ece = 0.0
    for bucket in bins:
        if not bucket:
            continue
        avg_p = mean(p for p, _ in bucket)
        acc = mean(y for _, y in bucket)
        ece += (len(bucket) / total) * abs(avg_p - acc)
    return round(ece, 4)


# ── Private helpers ───────────────────────────────────────────────────────


def _z_for_confidence(confidence: float) -> float:
    """Approximate z-critical values for common confidence levels."""
    table = {0.90: 1.6449, 0.95: 1.9600, 0.975: 2.2414, 0.99: 2.5758}
    # Snap to the nearest supported level; good enough for reporting.
    nearest = min(table.keys(), key=lambda k: abs(k - confidence))
    return table[nearest]


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))
