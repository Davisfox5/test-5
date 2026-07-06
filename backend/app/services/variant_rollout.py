"""Variant rollout management — promote / retire variants based on quality.

Biweekly Celery task (Tue/Fri 04:00 UTC).  For each ``running`` experiment:

1. Pull all quality scores for the control + treatment variants since
   ``Experiment.start_date``.
2. Require ≥ 200 evaluated items per variant before deciding anything.
3. Decide with Welch's t-test + a minimum practical effect — a raw
   ``delta > 0`` on imbalanced canary-vs-active samples is noise, not a
   winner.  Significant improvement → ``ready_for_review`` (Gate 2 —
   humans approve the actual promotion) + a webhook so the gate is seen.
   Significant regression → concluded, treatment retired.  No detectable
   difference → keep collecting until :data:`MAX_SAMPLES_PER_VARIANT`,
   then conclude "no practical difference" (status quo wins).

This is **explicitly not** a fully-automated rollout.  Per the plan there's
always a human gate — no auto-promotion of new variants without sign-off.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from backend.app.models import Experiment, InsightQualityScore, PromptVariant, Tenant
from backend.app.services.stats import welch_t_test

logger = logging.getLogger(__name__)

MIN_SAMPLES_PER_VARIANT = 200
# Stop collecting and call it a draw once both arms have this many samples —
# an undetectable difference at this n is not worth more judge spend.
MAX_SAMPLES_PER_VARIANT = 2000
# Composite quality scores live on 0–1; differences below a point aren't
# worth a prompt migration even when statistically detectable.
MIN_EFFECT_DELTA = 0.01
SIGNIFICANCE_P = 0.05


def _scores_for_variant_in_tenant(
    session: Session, tenant_id, variant_id, since: datetime
) -> list:
    """Raw quality scores for one variant, scoped to a single tenant.

    ``InsightQualityScore`` is tenant-scoped (RLS-protected); the experiment
    itself isn't, so the caller binds ``tenant_context`` per tenant and
    merges the raw values before computing statistics.
    """
    return [
        float(s)
        for (s,) in (
            session.query(InsightQualityScore.score)
            .filter(InsightQualityScore.tenant_id == tenant_id)
            .filter(InsightQualityScore.prompt_variant_id == variant_id)
            .filter(InsightQualityScore.created_at >= since)
            .all()
        )
        if s is not None
    ]


def _stats_from_values(values: list) -> Tuple[int, Optional[float], float]:
    """(n, mean, variance) of an already-collected list of quality scores."""
    n = len(values)
    if n == 0:
        return 0, None, 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    return n, mean, var


def _stats_for_variant_all_tenants(
    session: Session, tenants: list, variant_id, since: datetime
) -> Tuple[int, Optional[float], float]:
    """(n, mean, variance) of quality scores for one variant, across every
    tenant — ``Experiment``/``PromptVariant`` are global, so a variant's
    scores can span tenants and each tenant's rows must be read under its
    own RLS context.
    """
    from backend.app.tenant_ctx import tenant_context

    values: list = []
    for tenant in tenants:
        with tenant_context(tenant.id, session):
            values.extend(
                _scores_for_variant_in_tenant(session, tenant.id, variant_id, since)
            )
    return _stats_from_values(values)


def _notify_ready_for_review(
    session: Session, exp: Experiment, result: Dict[str, Any]
) -> None:
    """Surface the human gate — a winner nobody sees never ships."""
    try:
        from backend.app.services.webhook_dispatcher import dispatch_sync

        dispatch_sync(
            session,
            tenant_id=None,
            event="quality.alert",
            payload={
                "event": "experiment.ready_for_review",
                "experiment_id": str(exp.id),
                "experiment_name": exp.name,
                "message": (
                    f"Experiment '{exp.name}' has a winner waiting on your "
                    "approval. Review and promote it from the experiments "
                    "admin page — until then the improvement isn't live."
                ),
                **result,
            },
        )
    except Exception:
        logger.exception("ready_for_review webhook failed (non-fatal)")


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

    tenants = session.query(Tenant).all()

    for exp in experiments:
        if not exp.control_variant_id or not exp.treatment_variant_id:
            continue
        c_n, c_avg, c_var = _stats_for_variant_all_tenants(
            session, tenants, exp.control_variant_id, exp.start_date
        )
        t_n, t_avg, t_var = _stats_for_variant_all_tenants(
            session, tenants, exp.treatment_variant_id, exp.start_date
        )

        if c_n < MIN_SAMPLES_PER_VARIANT or t_n < MIN_SAMPLES_PER_VARIANT:
            not_ready += 1
            continue

        if c_avg is None or t_avg is None:
            not_ready += 1
            continue

        delta = t_avg - c_avg
        _, p_value = welch_t_test(t_avg, t_var, t_n, c_avg, c_var, c_n)
        significant = p_value < SIGNIFICANCE_P and abs(delta) >= MIN_EFFECT_DELTA
        result = {
            "control_n": c_n,
            "control_avg": round(c_avg, 4),
            "treatment_n": t_n,
            "treatment_avg": round(t_avg, 4),
            "delta": round(delta, 4),
            "p_value": round(p_value, 4),
        }
        if significant and delta > 0.0:
            # Mark experiment ready for human review; set the treatment
            # variant to the same status (Gate 2 promotes it explicitly).
            exp.status = "ready_for_review"
            exp.end_date = datetime.utcnow()
            exp.result_summary = {**result, "winner": "treatment"}
            exp.conclusion = (
                f"Treatment {exp.treatment_variant_id} beats control "
                f"by {delta:+.4f} (p={p_value:.4f}).  Awaiting human approval."
            )
            tv = session.query(PromptVariant).filter(
                PromptVariant.id == exp.treatment_variant_id
            ).first()
            if tv is not None and tv.status not in ("active", "rolled_back", "retired"):
                tv.status = "ready_for_review"
            _notify_ready_for_review(session, exp, result)
            declared += 1
        elif significant:
            exp.status = "concluded"
            exp.end_date = datetime.utcnow()
            exp.result_summary = {**result, "winner": "control"}
            exp.conclusion = (
                f"Treatment scored below control (delta {delta:+.4f}, "
                f"p={p_value:.4f}).  Treatment will be retired."
            )
            tv = session.query(PromptVariant).filter(
                PromptVariant.id == exp.treatment_variant_id
            ).first()
            if tv is not None:
                tv.status = "retired"
                tv.retired_at = datetime.utcnow()
            inconclusive += 1
        elif c_n >= MAX_SAMPLES_PER_VARIANT and t_n >= MAX_SAMPLES_PER_VARIANT:
            # Enough data to be confident there's nothing here — the status
            # quo wins ties, so retire the treatment and free the slot.
            exp.status = "concluded"
            exp.end_date = datetime.utcnow()
            exp.result_summary = {**result, "winner": "control"}
            exp.conclusion = (
                f"No practical difference after {t_n} samples per arm "
                f"(delta {delta:+.4f}, p={p_value:.4f}).  Treatment retired."
            )
            tv = session.query(PromptVariant).filter(
                PromptVariant.id == exp.treatment_variant_id
            ).first()
            if tv is not None:
                tv.status = "retired"
                tv.retired_at = datetime.utcnow()
            inconclusive += 1
        else:
            # Not significant yet and still under the sample cap — keep
            # collecting rather than crowning a noise-level winner.
            not_ready += 1

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
