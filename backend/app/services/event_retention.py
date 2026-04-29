"""Retention sweeps for high-volume event tables.

Two sweeps, both idempotent and safe to run daily:

* ``webhook_deliveries`` — delete rows whose ``delivered_at`` is older
  than ``WEBHOOK_DELIVERY_RETENTION_DAYS`` (default 90). Pending /
  in-retry rows are always preserved regardless of age. Dead-letter
  rows are dropped on the same window — the admin dead-letter UI only
  shows the last 30 days anyway.

* ``feedback_events`` — aggregate rows older than
  ``FEEDBACK_EVENT_RAW_RETENTION_DAYS`` (default 365) into
  ``feedback_daily_rollup`` grouped by day/surface/event_type, then
  delete the raw rows. The rollup survives indefinitely so calibration
  still sees historical volume.

Per-tenant overrides
--------------------

Both windows can be overridden per-tenant via
``Tenant.retention_days_webhook_deliveries`` and
``Tenant.retention_days_feedback_events`` (NULL ⇒ system default). The
sweep splits the work by tenant: tenants with a custom threshold run
under their own value, the rest fall through to the global default in
one bulk pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    FeedbackDailyRollup,
    FeedbackEvent,
    Tenant,
    WebhookDelivery,
)

logger = logging.getLogger(__name__)


# System defaults — per-tenant overrides on ``Tenant`` win when set.
WEBHOOK_DELIVERY_RETENTION_DAYS = 90
# Audit asked for 365d on feedback events; bumping from the prior 180d
# default so the rollup rolls less frequently on quiet tenants.
FEEDBACK_EVENT_RAW_RETENTION_DAYS = 365


@dataclass
class SweepResult:
    deliveries_deleted: int = 0
    feedback_events_rolled_up: int = 0
    feedback_events_deleted: int = 0


async def _tenant_retention_overrides(
    db: AsyncSession, column
) -> dict:
    """Return ``{tenant_id: days}`` for tenants with a non-NULL override."""
    rows = await db.execute(
        select(Tenant.id, column).where(column.is_not(None))
    )
    return {tid: days for tid, days in rows.all() if days is not None}


async def sweep_webhook_deliveries(
    db: AsyncSession, retention_days: int = WEBHOOK_DELIVERY_RETENTION_DAYS
) -> int:
    """Delete delivered / dead-letter rows older than ``retention_days``.

    Pending and failed-but-retrying rows are preserved regardless of age —
    those still carry state the dispatcher cares about. Tenants that set
    ``Tenant.retention_days_webhook_deliveries`` override the global
    ``retention_days`` argument.
    """
    overrides = await _tenant_retention_overrides(
        db, Tenant.retention_days_webhook_deliveries
    )
    deleted_total = 0

    # Custom-threshold tenants first — each scoped to its own row set.
    for tenant_id, override_days in overrides.items():
        cutoff = datetime.now(timezone.utc) - timedelta(days=override_days)
        stmt = (
            delete(WebhookDelivery)
            .where(WebhookDelivery.tenant_id == tenant_id)
            .where(WebhookDelivery.created_at < cutoff)
            .where(WebhookDelivery.status.in_(("sent", "dead_letter")))
            .execution_options(synchronize_session=False)
        )
        result = await db.execute(stmt)
        deleted_total += result.rowcount or 0

    # Everyone else — one bulk pass against the global default.
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stmt = (
        delete(WebhookDelivery)
        .where(WebhookDelivery.created_at < cutoff)
        .where(WebhookDelivery.status.in_(("sent", "dead_letter")))
        .execution_options(synchronize_session=False)
    )
    if overrides:
        stmt = stmt.where(~WebhookDelivery.tenant_id.in_(list(overrides.keys())))
    result = await db.execute(stmt)
    deleted_total += result.rowcount or 0

    await db.commit()
    if deleted_total:
        logger.info(
            "webhook_deliveries retention sweep deleted %d rows "
            "(default=%dd, %d tenant overrides)",
            deleted_total,
            retention_days,
            len(overrides),
        )
    return deleted_total


async def _sweep_feedback_events_pass(
    db: AsyncSession,
    cutoff: datetime,
    *,
    only_tenant_id=None,
    exclude_tenant_ids=None,
) -> tuple[int, int]:
    """Roll up + delete one slice of ``feedback_events`` < ``cutoff``.

    Either scopes to a single tenant (``only_tenant_id``) or excludes a
    list (``exclude_tenant_ids``) — never both. Returns
    ``(rolled_up_rows, deleted_rows)`` for that slice.
    """
    agg_stmt = (
        select(
            FeedbackEvent.tenant_id,
            func.date(FeedbackEvent.created_at).label("day"),
            FeedbackEvent.surface,
            FeedbackEvent.event_type,
            func.count().label("count"),
        )
        .where(FeedbackEvent.created_at < cutoff)
        .group_by(
            FeedbackEvent.tenant_id,
            func.date(FeedbackEvent.created_at),
            FeedbackEvent.surface,
            FeedbackEvent.event_type,
        )
    )
    if only_tenant_id is not None:
        agg_stmt = agg_stmt.where(FeedbackEvent.tenant_id == only_tenant_id)
    elif exclude_tenant_ids:
        agg_stmt = agg_stmt.where(~FeedbackEvent.tenant_id.in_(exclude_tenant_ids))
    agg_rows = (await db.execute(agg_stmt)).all()

    rolled_up = 0
    for tenant_id, day, surface, event_type, count in agg_rows:
        if count == 0:
            continue
        # Upsert: add to any existing row for the same (tenant, day, surface,
        # event_type) — safe if the sweep retries or runs overlap.
        stmt = (
            pg_insert(FeedbackDailyRollup)
            .values(
                tenant_id=tenant_id,
                day=day,
                surface=surface,
                event_type=event_type,
                count=count,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "day", "surface", "event_type"],
                set_={
                    "count": FeedbackDailyRollup.__table__.c.count + count,
                },
            )
        )
        await db.execute(stmt)
        rolled_up += 1

    del_stmt = (
        delete(FeedbackEvent)
        .where(FeedbackEvent.created_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    if only_tenant_id is not None:
        del_stmt = del_stmt.where(FeedbackEvent.tenant_id == only_tenant_id)
    elif exclude_tenant_ids:
        del_stmt = del_stmt.where(~FeedbackEvent.tenant_id.in_(exclude_tenant_ids))
    del_result = await db.execute(del_stmt)
    return rolled_up, del_result.rowcount or 0


async def sweep_feedback_events(
    db: AsyncSession, retention_days: int = FEEDBACK_EVENT_RAW_RETENTION_DAYS
) -> tuple[int, int]:
    """Roll expired feedback_events into feedback_daily_rollup, then delete.

    Tenants with ``Tenant.retention_days_feedback_events`` override the
    global ``retention_days``; the rest fall through to one bulk pass.
    Returns ``(rolled_up_rows, deleted_rows)`` summed across both.
    """
    overrides = await _tenant_retention_overrides(
        db, Tenant.retention_days_feedback_events
    )

    rolled_total = 0
    deleted_total = 0

    for tenant_id, override_days in overrides.items():
        cutoff = datetime.now(timezone.utc) - timedelta(days=override_days)
        rolled, deleted = await _sweep_feedback_events_pass(
            db, cutoff, only_tenant_id=tenant_id
        )
        rolled_total += rolled
        deleted_total += deleted

    # Catch-all pass under the system default.
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    rolled, deleted = await _sweep_feedback_events_pass(
        db, cutoff, exclude_tenant_ids=list(overrides.keys())
    )
    rolled_total += rolled
    deleted_total += deleted

    await db.commit()
    if deleted_total:
        logger.info(
            "feedback_events retention sweep: deleted %d rows, %d aggregate "
            "rows written (default=%dd, %d tenant overrides)",
            deleted_total,
            rolled_total,
            retention_days,
            len(overrides),
        )
    return rolled_total, deleted_total


async def run_event_retention_sweep(db: AsyncSession) -> Dict[str, Any]:
    """Run both sweeps; return a summary dict for the Celery task result."""
    result = SweepResult()
    result.deliveries_deleted = await sweep_webhook_deliveries(db)
    result.feedback_events_rolled_up, result.feedback_events_deleted = (
        await sweep_feedback_events(db)
    )
    return {
        "deliveries_deleted": result.deliveries_deleted,
        "feedback_events_rolled_up": result.feedback_events_rolled_up,
        "feedback_events_deleted": result.feedback_events_deleted,
    }
