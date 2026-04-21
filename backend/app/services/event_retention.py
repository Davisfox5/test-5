"""Retention sweeps for high-volume event tables.

Two sweeps, both idempotent and safe to run daily:

* ``webhook_deliveries`` — delete rows whose ``delivered_at`` is older
  than ``WEBHOOK_DELIVERY_RETENTION_DAYS`` (default 90). Pending /
  in-retry rows are always preserved regardless of age. Dead-letter
  rows are dropped on the same window — the admin dead-letter UI only
  shows the last 30 days anyway.

* ``feedback_events`` — aggregate rows older than
  ``FEEDBACK_EVENT_RAW_RETENTION_DAYS`` (default 180) into
  ``feedback_daily_rollup`` grouped by day/surface/event_type, then
  delete the raw rows. The rollup survives indefinitely so calibration
  still sees historical volume.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import FeedbackDailyRollup, FeedbackEvent, WebhookDelivery

logger = logging.getLogger(__name__)


WEBHOOK_DELIVERY_RETENTION_DAYS = 90
FEEDBACK_EVENT_RAW_RETENTION_DAYS = 180


@dataclass
class SweepResult:
    deliveries_deleted: int = 0
    feedback_events_rolled_up: int = 0
    feedback_events_deleted: int = 0


async def sweep_webhook_deliveries(
    db: AsyncSession, retention_days: int = WEBHOOK_DELIVERY_RETENTION_DAYS
) -> int:
    """Delete delivered / dead-letter rows older than ``retention_days``.

    Pending and failed-but-retrying rows are preserved regardless of age —
    those still carry state the dispatcher cares about.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stmt = (
        delete(WebhookDelivery)
        .where(WebhookDelivery.created_at < cutoff)
        .where(WebhookDelivery.status.in_(("sent", "dead_letter")))
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    deleted = result.rowcount or 0
    await db.commit()
    if deleted:
        logger.info(
            "webhook_deliveries retention sweep deleted %d rows older than %d days",
            deleted,
            retention_days,
        )
    return deleted


async def sweep_feedback_events(
    db: AsyncSession, retention_days: int = FEEDBACK_EVENT_RAW_RETENTION_DAYS
) -> tuple[int, int]:
    """Roll expired feedback_events into feedback_daily_rollup, then delete.

    Returns ``(rolled_up_rows, deleted_rows)`` where ``rolled_up_rows`` is
    the number of (tenant_id, day, surface, event_type) aggregate rows
    written. Uses ``ON CONFLICT DO UPDATE`` so repeated runs against a
    partial state are idempotent.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_date: date = cutoff.date()

    # 1. Aggregate rows that are about to be deleted.
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

    # 2. Drop the raw rows.
    del_stmt = (
        delete(FeedbackEvent)
        .where(FeedbackEvent.created_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    del_result = await db.execute(del_stmt)
    deleted = del_result.rowcount or 0

    await db.commit()
    if deleted:
        logger.info(
            "feedback_events retention sweep: %d raw rows > %d days rolled "
            "into %d aggregate rows, then deleted",
            deleted,
            retention_days,
            rolled_up,
        )
    return rolled_up, deleted


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
