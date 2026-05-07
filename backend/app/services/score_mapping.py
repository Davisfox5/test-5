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


def derive_numeric_scores(insights: Dict[str, Any]) -> Dict[str, Any]:
    """Populate numeric score fields from their bucket counterparts.

    Mutates the insights dict in place AND returns it for convenience.
    Always overwrites the numeric field — the bucket is the source of
    truth, and the LLM may not even be emitting numerics anymore.

    A bucket without a mapping (model invented a new label) leaves the
    numeric field at its previous value or unset; we don't guess.
    """
    sentiment_bucket = insights.get("sentiment_overall")
    sentiment_score = map_sentiment(sentiment_bucket)
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