"""Deterministic rubric scoring from structured evidence.

Phase 3 foundation: the LLM emits an ``evidence`` block of typed,
counted facts grounded in the transcript (objections, commitments,
discovery questions, competitor mentions, etc.). This module derives
deterministic rubric scores from that evidence — sitting alongside the
LLM's coarse buckets rather than replacing them, so we can dual-log
LLM-bucket vs rubric-score and validate calibration before flipping.

Once Phase 4 lands a calibrated classifier, this rubric becomes the
cold-start fallback for tenants without enough labels.

The scores produced here are written to ``Interaction.insights.rubric``
and are intentionally separate from the existing
``sentiment_score`` / ``churn_risk`` / ``upsell_score`` fields, which
keep their bucket-mapped values from ``score_mapping.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ── Tunable rubric constants ──────────────────────────────────────────
# These thresholds are not "tuned" in any data-driven sense yet. They
# encode the team's qualitative read of what counts as a strong
# discovery call / a high-quality close. Phase 4 will replace them with
# a fitted model.

DISCOVERY_QUESTIONS_FULL_CREDIT = 8
COMMITMENTS_FULL_CREDIT = 3
OBJECTION_RESOLUTION_THRESHOLD_RATIO = 0.7
COMPETITOR_MENTION_PENALTY = 0.10  # subtracted from win-likelihood per
# mention (capped at 3 mentions)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return min(1.0, numerator / denominator)


def compute_rubric(evidence: Dict[str, Any]) -> Dict[str, float]:
    """Translate an evidence block into rubric scores.

    Returns a flat dict with floats in [0, 1]. Empty evidence yields
    all-zero scores (the safe / cold-start signal — no evidence found,
    no claim made).
    """
    objection_count = _safe_int(evidence.get("objection_count"))
    unresolved = _safe_int(evidence.get("unresolved_objection_count"))
    commitments = _safe_int(evidence.get("commitment_count"))
    questions = _safe_int(evidence.get("discovery_questions"))
    competitor_mentions = _safe_int(evidence.get("competitor_mention_count"))

    discovery_quality = _ratio(questions, DISCOVERY_QUESTIONS_FULL_CREDIT)
    commitment_strength = _ratio(commitments, COMMITMENTS_FULL_CREDIT)

    if objection_count > 0:
        resolved = max(0, objection_count - unresolved)
        resolution_rate = resolved / objection_count
    else:
        # No objections raised → we can't claim "great handling". Treat
        # absence as neutral.
        resolution_rate = 0.5

    # Win-likelihood: a mash-up of discovery + commitment + resolution,
    # penalised by competitor air-time. Caps and floors keep it sane.
    raw_win = (
        0.4 * discovery_quality
        + 0.3 * commitment_strength
        + 0.3 * resolution_rate
        - COMPETITOR_MENTION_PENALTY * min(competitor_mentions, 3)
    )
    win_likelihood = max(0.0, min(1.0, raw_win))

    return {
        "discovery_quality": round(discovery_quality, 3),
        "commitment_strength": round(commitment_strength, 3),
        "objection_resolution_rate": round(resolution_rate, 3),
        "win_likelihood": round(win_likelihood, 3),
    }


def attach_rubric(insights: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Compute the rubric from ``insights['evidence']`` and write it to
    ``insights['rubric']``. No-op when the LLM emitted no evidence
    block. Returns the rubric dict (or None) for the caller's
    convenience.
    """
    evidence = insights.get("evidence")
    if not isinstance(evidence, dict):
        return None
    rubric = compute_rubric(evidence)
    insights["rubric"] = rubric
    return rubric