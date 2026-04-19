"""Variant rollout management — promote / retire variants based on quality.

Biweekly Celery task (Tue/Fri 04:00 UTC).  For each ``running`` experiment:

1. Pull all quality scores for the control + treatment variants since
   ``Experiment.start_date``.
2. Require ≥ 200 evaluated items per variant before declaring a winner.
3. If treatment composite > control composite, mark the experiment
   ``ready_for_review`` (Gate 2 — humans approve the actual promotion).
4. Otherwise mark ``concluded`` with a "no winner" conclusion.

This is **explicitly not** a fully-automated rollout.  Per the plan there's
always a human gate — no auto-promotion of new variants without sign-off.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.models import Experiment, InsightQualityScore, PromptVariant

logger = logging.getLogger(__name__)

MIN_SAMPLES_PER_VARIANT = 200


def _stats_for_variant(
    session: Session, variant_id, since: datetime
) -> Tuple[int, Optional[float]]:
    n = (
        session.query(func.count(InsightQualityScore.id))
        .filter(InsightQualityScore.prompt_variant_id == variant_id)
        .filter(InsightQualityScore.created_at >= since)
        .scalar()
    ) or 0
    avg = (
        session.query(func.avg(InsightQualityScore.score))
        .filter(InsightQualityScore.prompt_variant_id == variant_id)
        .filter(InsightQualityScore.created_at >= since)
        .scalar()
    )
    return int(n), float(avg) if avg is not None else None


def evaluate_active_experiments(session: Session) -> Dict[str, Any]:
    experiments = (
        session.query(Experiment)
        .filter(Experiment.status == "running")
        .filter(Experiment.type == "prompt_ab_test")
        .all()
    )

    declared = 0
    inconclusive = 0
    not_ready = 0

    for exp in experiments:
        if not exp.control_variant_id or not exp.treatment_variant_id:
            continue
        c_n, c_avg = _stats_for_variant(session, exp.control_variant_id, exp.start_date)
        t_n, t_avg = _stats_for_variant(session, exp.treatment_variant_id, exp.start_date)

        if c_n < MIN_SAMPLES_PER_VARIANT or t_n < MIN_SAMPLES_PER_VARIANT:
            not_ready += 1
            continue

        if c_avg is None or t_avg is None:
            not_ready += 1
            continue

        delta = t_avg - c_avg
        result = {
            "control_n": c_n,
            "control_avg": round(c_avg, 4),
            "treatment_n": t_n,
            "treatment_avg": round(t_avg, 4),
            "delta": round(delta, 4),
        }
        if delta > 0.0:
            # Mark experiment ready for human review; set the treatment
            # variant to the same status (Gate 2 promotes it explicitly).
            exp.status = "ready_for_review"
            exp.end_date = datetime.utcnow()
            exp.result_summary = {**result, "winner": "treatment"}
            exp.conclusion = (
                f"Treatment {exp.treatment_variant_id} beats control "
                f"by {delta:+.4f}.  Awaiting human approval."
            )
            tv = session.query(PromptVariant).filter(
                PromptVariant.id == exp.treatment_variant_id
            ).first()
            if tv is not None and tv.status not in ("active", "rolled_back", "retired"):
                tv.status = "ready_for_review"
            declared += 1
        else:
            exp.status = "concluded"
            exp.end_date = datetime.utcnow()
            exp.result_summary = {**result, "winner": "control"}
            exp.conclusion = (
                f"Treatment did not beat control (delta {delta:+.4f}).  "
                f"Treatment will be retired."
            )
            tv = session.query(PromptVariant).filter(
                PromptVariant.id == exp.treatment_variant_id
            ).first()
            if tv is not None:
                tv.status = "retired"
                tv.retired_at = datetime.utcnow()
            inconclusive += 1

    if declared or inconclusive:
        session.commit()
        # Bust the variant cache so promotions take effect quickly.
        try:
            from backend.app.services.prompt_variant_service import bust_cache

            bust_cache()
        except Exception:
            logger.exception("Variant cache bust failed (non-fatal)")
    return {
        "experiments_evaluated": len(experiments),
        "winners_declared": declared,
        "inconclusive": inconclusive,
        "not_ready": not_ready,
    }
