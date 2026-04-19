"""Quality regression watchdog.

Hourly Celery task.  For each prompt variant currently in shadow / canary /
active rollout, compares the 24-hour rolling average composite score against
the prior 7-day baseline.  If the drop > 5%:

1. Suspends any A/B experiment for that variant that started in the last 48h.
2. Fires a ``quality.alert`` webhook (Slack via the dispatcher).

Per the plan we deliberately do **not** auto-rollback the variant — an
engineer makes that call so we don't paper over a real bug.
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
)

logger = logging.getLogger(__name__)

REGRESSION_THRESHOLD = 0.05  # 5%


def _avg_score_for_variant(
    session: Session, variant_id: _uuid.UUID, since: datetime
) -> float:
    avg = (
        session.query(func.avg(InsightQualityScore.score))
        .filter(InsightQualityScore.prompt_variant_id == variant_id)
        .filter(InsightQualityScore.created_at >= since)
        .scalar()
    )
    return float(avg) if avg is not None else 0.0


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
    alerts = 0
    suspended = 0
    for variant in variants:
        recent = _avg_score_for_variant(session, variant.id, last_24h)
        baseline_avg = (
            session.query(func.avg(InsightQualityScore.score))
            .filter(InsightQualityScore.prompt_variant_id == variant.id)
            .filter(InsightQualityScore.created_at >= last_7d_start)
            .filter(InsightQualityScore.created_at < last_7d_end)
            .scalar()
        )
        baseline = float(baseline_avg) if baseline_avg is not None else 0.0
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

        alerts += 1
        try:
            from backend.app.services.webhook_dispatcher import dispatch_sync

            dispatch_sync(
                session,
                tenant_id=None,
                event="quality.alert",
                payload={
                    "event": "quality.regression",
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

    if alerts or suspended:
        session.commit()
    return {
        "checked_variants": len(variants),
        "alerts_emitted": alerts,
        "experiments_suspended": suspended,
    }
