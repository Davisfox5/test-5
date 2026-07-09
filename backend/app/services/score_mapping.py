"""Bucket → number mapping for analysis scores.

Phase 1 of the calibration plan: the LLM emits coarse buckets only
(``sentiment_overall``, ``churn_risk_signal``, ``upsell_signal``,
``script_adherence_band``) and we map those to the numeric fields
downstream consumers (analytics, contact health, dashboards, training
data) still expect. This eliminates the "false precision" of LLM-emitted
floats while keeping the API stable.

The mapping centers each bucket and is deliberately deterministic.
Downstream classifier work (Phase 4) will train a calibrated model on
top of evidence features, at which point this mapping becomes the
cold-start fallback.

Aspect-based sentiment upgrade: the analyzer now ALSO emits
``sentiment_score_direct``, a continuous 0-10 read of the call that
isn't derived from the ``sentiment_overall`` bucket. ``sentiment_score``
prefers that real gradient when it's a valid number in range, and only
falls back to the bucket anchor above when it's absent or malformed
(older prompt variant, a flaky response). ``score_engine.py`` and every
other downstream reader still just consumes ``insights["sentiment_score"]``
— this module is the only place that needs to know which source won.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ── Bucket → number tables ────────────────────────────────────────────

# sentiment_overall → sentiment_score (0–10 scale; 10 = most positive).
# "mixed" sits between neutral and negative because the rep heard real
# pushback even if the call ended civil.
SENTIMENT_BUCKET_TO_SCORE: Dict[str, float] = {
    "positive": 8.5,
    "neutral": 6.0,
    "mixed": 4.5,
    "negative": 2.5,
}

# churn_risk_signal / upsell_signal → 0.0–1.0 score. These mirror the
# old prompt-comment values so historical analytics stay comparable
# (the LLM was already aiming for these centers when it emitted the
# decimal).
RISK_BUCKET_TO_SCORE: Dict[str, float] = {
    "high": 0.85,
    "medium": 0.55,
    "low": 0.25,
    "none": 0.05,
}

# script_adherence_band → 0–100. Bands aren't pegged to round numbers
# so a downstream "≥ 80" filter still cleanly excludes the "medium"
# bucket.
SCRIPT_ADHERENCE_BAND_TO_SCORE: Dict[str, float] = {
    "high": 92.0,
    "medium": 75.0,
    "low": 55.0,
    "failing": 30.0,
}


def map_sentiment(bucket: Optional[str]) -> Optional[float]:
    if not bucket:
        return None
    return SENTIMENT_BUCKET_TO_SCORE.get(bucket.lower().strip())


def map_risk(bucket: Optional[str]) -> Optional[float]:
    if not bucket:
        return None
    return RISK_BUCKET_TO_SCORE.get(bucket.lower().strip())


def map_script_adherence(band: Optional[str]) -> Optional[float]:
    if not band:
        return None
    return SCRIPT_ADHERENCE_BAND_TO_SCORE.get(band.lower().strip())


def resolve_sentiment_score(insights: Dict[str, Any]) -> Optional[float]:
    """Resolve the numeric sentiment score for one interaction.

    Prefers ``sentiment_score_direct`` (the continuous 0-10 read the
    analyzer emits alongside the coarse bucket) when it's present and a
    valid number in ``[0, 10]``. Falls back to the bucket-anchor map
    when it's absent, non-numeric, or out of range — the old behavior,
    kept as the safety net for older rows / prompt variants that never
    emitted the direct score.

    Scale-confusion guard: the analyzer occasionally emits the direct
    score on a **0–1 scale** (e.g. ``0.7`` for an enthusiastic call)
    instead of 0–10. A naive ``0 <= x <= 10`` check lets that through
    verbatim, so a genuinely positive prospect renders as ~0.7/10 and
    the frontend labels them "Negative" — the exact inversion reported
    for engaged prospects. We cross-check against the coarse bucket:
    when the direct read is ``<= 1.0`` but the bucket anchor says
    neutral-or-better (>= 4.0), the direct value is mis-scaled and we
    rescale ×10. We deliberately do NOT rescale when the bucket agrees
    the sentiment is low (``negative``), so a legitimately bad call
    scored ``0.7/10`` is preserved.
    """
    bucket_anchor = map_sentiment(insights.get("sentiment_overall"))
    direct = insights.get("sentiment_score_direct")
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        direct_f = float(direct)
        if 0.0 <= direct_f <= 10.0:
            if direct_f <= 1.0 and bucket_anchor is not None and bucket_anchor >= 4.0:
                direct_f *= 10.0
            return max(0.0, min(10.0, direct_f))
    return bucket_anchor


def derive_numeric_scores(insights: Dict[str, Any]) -> Dict[str, Any]:
    """Populate numeric score fields from their bucket counterparts.

    Mutates the insights dict in place AND returns it for convenience.
    Always overwrites the numeric field — the bucket (or the direct
    score, when valid) is the source of truth, and the LLM may not even
    be emitting numerics otherwise.

    A bucket without a mapping (model invented a new label) AND no
    valid direct score leaves the numeric field at its previous value
    or unset; we don't guess.
    """
    sentiment_score = resolve_sentiment_score(insights)
    if sentiment_score is not None:
        insights["sentiment_score"] = sentiment_score

    churn_bucket = insights.get("churn_risk_signal")
    churn_score = map_risk(churn_bucket)
    if churn_score is not None:
        insights["churn_risk"] = churn_score

    upsell_bucket = insights.get("upsell_signal")
    upsell_score = map_risk(upsell_bucket)
    if upsell_score is not None:
        insights["upsell_score"] = upsell_score

    coaching = insights.get("coaching")
    if isinstance(coaching, dict):
        band = coaching.get("script_adherence_band")
        adh_score = map_script_adherence(band)
        if adh_score is not None:
            coaching["script_adherence_score"] = adh_score

    return insights