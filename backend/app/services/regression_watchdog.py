"""Quality regression watchdog.

Hourly Celery task.  For each prompt variant currently in shadow / canary /
active rollout, compares the 24-hour rolling average composite score against
the prior 7-day baseline.  If the drop > 5%:

1. Suspends any A/B experiment for that variant that started in the last 48h.
2. Rolls back regressed **shadow / canary** variants automatically — those
   are pre-promotion trials serving ≤ 20% of traffic, and ending a failing
   trial early is the safe direction (promotion still has its human gate).
3. Fires a ``quality.alert`` webhook (Slack via the dispatcher) with a
   plain-language message saying what happened and what to do next.

Per the plan we deliberately do **not** auto-rollback an **active** variant —
an engineer makes that call so we don't paper over a real bug.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from backend.app.models import (
    Experiment,
    InsightQualityScore,
    PromptVariant,
    Tenant,
)

logger = logging.getLogger(__name__)

REGRESSION_THRESHOLD = 0.05  # 5%


def _sum_count_for_variant(
    session: Session,
    tenant_id: _uuid.UUID,
    variant_id: _uuid.UUID,
    since: datetime,
    until: Any = None,
) -> Any:
    """(sum, count) of quality scores for one variant, scoped to one tenant."""
    q = (
        session.query(
            func.sum(InsightQualityScore.score), func.count(InsightQualityScore.score)
        )
        .filter(InsightQualityScore.tenant_id == tenant_id)
        .filter(InsightQualityScore.prompt_variant_id == variant_id)
        .filter(InsightQualityScore.created_at >= since)
    )
    if until is not None:
        q = q.filter(InsightQualityScore.created_at < until)
    total, count = q.one()
    return (float(total) if total is not None else 0.0), int(count or 0)


def _avg_score_for_variant(
    session: Session,
    tenants: List[Tenant],
    variant_id: _uuid.UUID,
    since: datetime,
    until: Any = None,
) -> float:
    """Average quality score for one variant across every tenant.

    ``PromptVariant``/``Experiment`` are global, so a variant's scores can
    span tenants — ``InsightQualityScore`` is tenant-scoped (RLS-protected),
    so each tenant's rows are read under its own bound context and combined
    as a count-weighted mean.
    """
    from backend.app.tenant_ctx import tenant_context

    total = 0.0
    count = 0
    for tenant in tenants:
        with tenant_context(tenant.id, session):
            t_total, t_count = _sum_count_for_variant(
                session, tenant.id, variant_id, since, until
            )
            total += t_total
            count += t_count
    return total / count if count else 0.0


def check_all_active_rollouts(session: Session) -> Dict[str, Any]:
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)
    last_7d_start = now - timedelta(days=7)
    last_7d_end = now - timedelta(hours=24)
    suspend_cutoff = now - timedelta(hours=48)

    variants = (
        session.query(PromptVariant)
        .filter(PromptVariant.status.in_(("shadow", "canary", "active")))
        .all()
    )
    tenants = session.query(Tenant).all()
    alerts = 0
    suspended = 0
    rolled_back = 0
    for variant in variants:
        recent = _avg_score_for_variant(session, tenants, variant.id, last_24h)
        baseline = _avg_score_for_variant(
            session, tenants, variant.id, last_7d_start, last_7d_end
        )
        if baseline <= 0:
            continue  # not enough data yet
        drop = baseline - recent
        if drop / max(baseline, 1e-6) <= REGRESSION_THRESHOLD:
            continue

        # Suspend any A/B experiments started in the last 48h that touch this variant.
        for exp in (
            session.query(Experiment)
            .filter(Experiment.status == "running")
            .filter(Experiment.start_date >= suspend_cutoff)
            .filter(
                or_(
                    Experiment.control_variant_id == variant.id,
                    Experiment.treatment_variant_id == variant.id,
                )
            )
            .all()
        ):
            exp.status = "suspended"
            exp.conclusion = (
                f"Auto-suspended: variant {variant.id} regressed "
                f"{drop / baseline:.2%} (recent={recent:.4f} vs baseline={baseline:.4f})"
            )
            suspended += 1

        # Trial variants (shadow/canary) roll back automatically — a
        # regressing trial should not keep serving traffic until a human
        # notices.  Active variants stay live: only an engineer decides
        # to roll back the default for every tenant.
        action_taken = "alert_only"
        if variant.status in ("shadow", "canary"):
            variant.status = "rolled_back"
            variant.retired_at = datetime.utcnow()
            action_taken = "trial_rolled_back"
            rolled_back += 1

        if action_taken == "trial_rolled_back":
            message = (
                f"Insight quality for the trial prompt '{variant.name}' dropped "
                f"{drop / baseline:.0%} over the last day, so the trial was "
                "stopped automatically. No customer-facing traffic is on it "
                "anymore. Review the variant before starting a new trial."
            )
        else:
            message = (
                f"Insight quality dropped {drop / baseline:.0%} over the last "
                f"day on the live prompt '{variant.name}'. Customers are "
                "seeing this now — review it on the experiments admin page "
                "and roll back if the drop is real."
            )

        alerts += 1
        try:
            from backend.app.services.webhook_dispatcher import dispatch_sync

            dispatch_sync(
                session,
                tenant_id=None,
                event="quality.alert",
                payload={
                    "event": "quality.regression",
                    "message": message,
                    "action_taken": action_taken,
                    "variant_id": str(variant.id),
                    "variant_name": variant.name,
                    "variant_status": variant.status,
                    "recent_24h_avg": round(recent, 4),
                    "baseline_7d_avg": round(baseline, 4),
                    "drop_pct": round(drop / baseline, 4),
                },
            )
        except Exception:
            logger.exception("Quality regression webhook failed (non-fatal)")

    if alerts or suspended or rolled_back:
        session.commit()
        # Rolled-back variants must leave the serving cache immediately.
        if rolled_back:
            try:
                from backend.app.services.prompt_variant_service import bust_cache

                bust_cache()
            except Exception:
                logger.exception("Variant cache bust failed (non-fatal)")
    return {
        "checked_variants": len(variants),
        "alerts_emitted": alerts,
        "experiments_suspended": suspended,
        "variants_rolled_back": rolled_back,
    }
