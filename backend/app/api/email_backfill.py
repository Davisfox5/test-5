"""Email backfill endpoints — historical mailbox import.

* ``POST /email/backfill`` — start a "sync the last N days" job for the
  tenant's connected Gmail / Outlook integration. Returns a job handle
  immediately; the ``email_backfill_run`` Celery task does the work on
  the batch queue. Re-posting while a job is queued/running returns the
  in-flight job instead of stacking a duplicate — backed by a partial
  unique index, not just the SELECT here, so concurrent POSTs can't
  race two jobs into existence.
* ``GET /email/backfill/{job_id}`` — poll progress (fetched / ingested /
  skipped / status). Consumers poll every few seconds until ``done`` or
  ``error``.

Stale-job handling: a job whose worker died without reaching a terminal
status must not lock the tenant out of backfill forever. A ``queued``
job that never started within ``QUEUED_STALE_AFTER``, or a ``running``
job whose worker heartbeat went silent for ``HEARTBEAT_STALE_AFTER``,
is superseded (marked ``error``) and a fresh job is started. A live job
tied to a *different* integration (the mailbox was reconnected while an
old sync is still running) returns 409 rather than risking two
concurrent sweeps of the same mailbox.

Scope: ``interactions:write`` on the POST (it creates Interaction rows);
the status GET is a plain authenticated read, matching the convention in
``docs/api_key_scope_map.yaml``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant, require_scope
from backend.app.db import get_db
from backend.app.models import (
    EMAIL_BACKFILL_HEARTBEAT_STALE_AFTER as HEARTBEAT_STALE_AFTER,
    EmailBackfillJob,
    Integration,
    Tenant,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# LINDA-side ceiling on the import window. Provider list APIs get slow and
# quota-hungry beyond this, and older threads add little signal.
MAX_WINDOW_DAYS = 90

# A job still 'queued' after this long was lost (broker flush, task
# dropped before a worker picked it up — no heartbeat ever gets stamped
# on those). The batch queue can back up, so this is deliberately
# generous; a superseded job that does eventually run bows out at the
# claim step because its status is no longer queued/running.
QUEUED_STALE_AFTER = timedelta(hours=1)

_EMAIL_PROVIDERS = ("google", "microsoft")


class BackfillStartRequest(BaseModel):
    provider: Literal["google", "microsoft"] = "google"
    days: int = Field(
        90,
        ge=1,
        le=MAX_WINDOW_DAYS,
        description=f"Trailing window to import, capped at {MAX_WINDOW_DAYS} days.",
    )


class BackfillStartResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    window_days: int


class BackfillJobOut(BaseModel):
    model_config = {"from_attributes": True}

    # Reads ``job.id`` off the ORM row; "job_id" is accepted too so
    # FastAPI's response-model revalidation of an already-built instance
    # round-trips.
    job_id: uuid.UUID = Field(validation_alias=AliasChoices("id", "job_id"))
    provider: str
    status: str
    window_days: int
    fetched: int
    ingested: int
    skipped: int
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes even for timezone=True columns."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _is_stale(job: EmailBackfillJob, now: datetime) -> bool:
    """True when the job's worker is presumed dead and it may be superseded."""
    if job.status == "queued":
        created = _aware(job.created_at) or now
        return created < now - QUEUED_STALE_AFTER
    # running: the worker stamps heartbeat_at at every checkpoint commit.
    last_beat = _aware(job.heartbeat_at) or _aware(job.started_at) or _aware(job.created_at) or now
    return last_beat < now - HEARTBEAT_STALE_AFTER


async def _in_flight_jobs(
    db: AsyncSession, tenant_id: uuid.UUID, provider: str
) -> List[EmailBackfillJob]:
    return list(
        (
            await db.execute(
                select(EmailBackfillJob).where(
                    EmailBackfillJob.tenant_id == tenant_id,
                    EmailBackfillJob.provider == provider,
                    EmailBackfillJob.status.in_(("queued", "running")),
                )
            )
        ).scalars().all()
    )


def _split_live_stale(
    jobs: List[EmailBackfillJob], now: datetime
) -> Tuple[List[EmailBackfillJob], List[EmailBackfillJob]]:
    live: List[EmailBackfillJob] = []
    stale: List[EmailBackfillJob] = []
    for job in jobs:
        (stale if _is_stale(job, now) else live).append(job)
    return live, stale


@router.post(
    "/email/backfill",
    response_model=BackfillStartResponse,
    status_code=202,
    dependencies=[Depends(require_scope("interactions:write"))],
)
async def start_email_backfill(
    body: BackfillStartRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Kick off a historical mailbox import for the connected mailbox.

    400 when the tenant has no connected integration for ``provider`` —
    connect the mailbox (OAuth) first.
    """
    integration = (
        await db.execute(
            select(Integration)
            .where(
                Integration.tenant_id == tenant.id,
                Integration.provider == body.provider,
            )
            .order_by(Integration.created_at.desc())
        )
    ).scalars().first()
    if integration is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No connected {body.provider} mailbox — connect one via "
                "OAuth before backfilling."
            ),
        )
    if (integration.provider_config or {}).get("needs_reauth"):
        raise HTTPException(
            status_code=400,
            detail="Mailbox credentials expired — reconnect the mailbox, then retry.",
        )

    now = datetime.now(timezone.utc)
    live, stale = _split_live_stale(
        await _in_flight_jobs(db, tenant.id, body.provider), now
    )

    for job in live:
        if job.integration_id == integration.id:
            # One in-flight job per (tenant, provider): re-posting returns
            # the existing handle so double-clicks and impatient reloads
            # don't stack duplicate provider sweeps.
            return BackfillStartResponse(
                job_id=job.id,
                status=job.status,
                window_days=job.window_days,
            )
        # A live sync is still running against a previously connected
        # integration for this provider. Starting a second sweep of the
        # same mailbox risks duplicate imports — wait it out (its
        # heartbeat goes stale within minutes if it actually died).
        raise HTTPException(
            status_code=409,
            detail=(
                "A sync for a previously connected mailbox is still "
                "running — try again in a few minutes."
            ),
        )

    # Dead jobs (lost task / crashed worker gone silent) must not lock
    # the tenant out of backfill forever: supersede them and start fresh.
    for job in stale:
        logger.warning(
            "Superseding stale backfill job %s (status=%s) for tenant %s",
            job.id, job.status, tenant.id,
        )
        job.status = "error"
        job.error = "The sync stalled and was superseded by a newer sync request."
        job.finished_at = now

    job = EmailBackfillJob(
        tenant_id=tenant.id,
        integration_id=integration.id,
        provider=body.provider,
        window_days=body.days,
        status="queued",
    )
    db.add(job)
    try:
        await db.flush()
        await db.commit()
    except IntegrityError:
        # Lost the partial-unique-index race to a concurrent POST — the
        # winner's job is the in-flight one; hand back its handle.
        await db.rollback()
        existing, _ = _split_live_stale(
            await _in_flight_jobs(db, tenant.id, body.provider),
            datetime.now(timezone.utc),
        )
        if existing:
            return BackfillStartResponse(
                job_id=existing[0].id,
                status=existing[0].status,
                window_days=existing[0].window_days,
            )
        raise HTTPException(
            status_code=409,
            detail="A sync was just started by another request — retry shortly.",
        )

    try:
        from backend.app.tasks import email_backfill_run

        email_backfill_run.delay(str(job.id))
    except Exception:  # noqa: BLE001 — Celery down ≠ silently-stuck job
        logger.exception("Could not enqueue email_backfill_run for job %s", job.id)
        job.status = "error"
        job.error = "Could not queue the sync job — try again shortly."
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=503, detail="Sync queue unavailable — try again shortly.")

    return BackfillStartResponse(job_id=job.id, status=job.status, window_days=job.window_days)


@router.get("/email/backfill/{job_id}", response_model=BackfillJobOut)
async def get_email_backfill(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Progress of one backfill job (tenant-scoped)."""
    job = (
        await db.execute(
            select(EmailBackfillJob).where(
                EmailBackfillJob.id == job_id,
                EmailBackfillJob.tenant_id == tenant.id,
            )
        )
    ).scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="Backfill job not found")
    return BackfillJobOut.model_validate(job)
