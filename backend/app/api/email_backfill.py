"""Email backfill endpoints — historical mailbox import.

* ``POST /email/backfill`` — start a "sync the last N days" job for the
  tenant's connected Gmail / Outlook integration. Returns a job handle
  immediately; the ``email_backfill_run`` Celery task does the work on
  the batch queue. Re-posting while a job is queued/running returns the
  in-flight job instead of stacking a duplicate.
* ``GET /email/backfill/{job_id}`` — poll progress (fetched / ingested /
  skipped / status). Consumers poll every few seconds until ``done`` or
  ``error``.

Scope: ``interactions:write`` on the POST (it creates Interaction rows);
the status GET is a plain authenticated read, matching the convention in
``docs/api_key_scope_map.yaml``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant, require_scope
from backend.app.db import get_db
from backend.app.models import EmailBackfillJob, Integration, Tenant

logger = logging.getLogger(__name__)
router = APIRouter()

# LINDA-side ceiling on the import window. Provider list APIs get slow and
# quota-hungry beyond this, and older threads add little signal.
MAX_WINDOW_DAYS = 90


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
    job_id: uuid.UUID
    provider: str
    status: str
    window_days: int
    fetched: int
    ingested: int
    skipped: int
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


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
) -> BackfillStartResponse:
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

    # One in-flight job per (tenant, provider): re-posting returns the
    # existing handle so double-clicks and impatient reloads don't stack
    # duplicate provider sweeps.
    in_flight = (
        await db.execute(
            select(EmailBackfillJob)
            .where(
                EmailBackfillJob.tenant_id == tenant.id,
                EmailBackfillJob.provider == body.provider,
                EmailBackfillJob.status.in_(("queued", "running")),
            )
            .order_by(EmailBackfillJob.created_at.desc())
        )
    ).scalars().first()
    if in_flight is not None:
        return BackfillStartResponse(
            job_id=in_flight.id,
            status=in_flight.status,
            window_days=in_flight.window_days,
        )

    job = EmailBackfillJob(
        tenant_id=tenant.id,
        integration_id=integration.id,
        provider=body.provider,
        window_days=body.days,
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Import here to avoid a circular import at module load (tasks pulls in
    # most of the service layer). Mirrors the email-push endpoint.
    try:
        from backend.app.tasks import email_backfill_run

        email_backfill_run.delay(str(job.id))
    except Exception:  # noqa: BLE001 — Celery down ≠ silently-stuck job
        logger.exception("Could not enqueue email_backfill_run for job %s", job.id)
        job.status = "error"
        job.error = "Could not queue the sync job — try again shortly."
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(
            status_code=503, detail="Sync queue unavailable — try again shortly."
        )

    return BackfillStartResponse(
        job_id=job.id, status=job.status, window_days=job.window_days
    )


@router.get("/email/backfill/{job_id}", response_model=BackfillJobOut)
async def get_email_backfill(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> BackfillJobOut:
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
    return BackfillJobOut(
        job_id=job.id,
        provider=job.provider,
        status=job.status,
        window_days=job.window_days,
        fetched=job.fetched,
        ingested=job.ingested,
        skipped=job.skipped,
        error=job.error,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )
