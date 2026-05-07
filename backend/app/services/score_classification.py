"""Score-field classification registry.

Phase 3 foundation: every numeric / bucketed field in
``Interaction.insights`` falls into one of three categories.

* **measurement** — directly counted from the transcript / call (rep
  talk-time ratio, number of objections, methodology stages covered).
  Deterministic: same input → same output. Should never come from an
  LLM bucket.
* **prediction** — forward-looking outcome estimate (churn risk, deal
  velocity, customer NPS shift). The LLM is a stop-gap until we have
  enough labeled outcome data to train a calibrated classifier; until
  then, buckets via ``score_mapping`` give us deterministic but coarse
  numerics.
* **subjective** — sentiment / tone / rep effort / coaching qualities.
  LLM-driven and likely to stay so; calibration here is a goal but not
  a blocker. Surface to users as buckets (positive / neutral / etc.)
  rather than decimals.

Use this registry when building dashboards, choosing UI affordances
(decimal vs bucket display), and auditing where the LLM is doing work
it shouldn't be doing.
"""

from __future__ import annotations

from typing import Dict, Literal

ScoreCategory = Literal["measurement", "prediction", "subjective"]

# Canonical field paths inside ``Interaction.insights`` mapped to their
# category. Nested fields are represented dot-delimited.
SCORE_FIELD_CLASSIFICATION: Dict[str, ScoreCategory] = {
    # --- subjective ---
    "sentiment_overall": "subjective",
    "sentiment_score": "subjective",
    "sentiment_trajectory": "subjective",
    "coaching.what_went_well": "subjective",
    "coaching.improvements": "subjective",
    # --- prediction ---
    "churn_risk_signal": "prediction",
    "churn_risk": "prediction",
    "upsell_signal": "prediction",
    "upsell_score": "prediction",
    # --- measurement ---
    "methodology_coverage.covered": "measurement",
    "methodology_coverage.missing": "measurement",
    "coaching.script_adherence_band": "measurement",
    "coaching.script_adherence_score": "measurement",
    "coaching.compliance_gaps": "measurement",
    "evidence.objection_count": "measurement",
    "evidence.commitment_count": "measurement",
    "evidence.discovery_questions": "measurement",
    "evidence.unresolved_objection_count": "measurement",
    "evidence.competitor_mention_count": "measurement",
}


def category_of(field: str) -> ScoreCategory:
    """Return the category for a known field, defaulting to ``subjective``
    so an unknown LLM-emitted score doesn't get treated as deterministic
    by accident.
    """
    return SCORE_FIELD_CLASSIFICATION.get(field, "subjective")