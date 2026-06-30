"""On-demand historical email backfill.

Forward ingestion (poller + ``/email-push/gmail``) only captures mail
that arrives after a mailbox is connected.  These endpoints let a tenant
pull the last N days of history and run it through the SAME
ingest→analyze pipeline, so backfilled mail produces identical
Interactions (channel=email, sentiment, action items, threading).

The work runs in a Celery worker (``email_backfill_run``) — the POST
returns 202 immediately.  Idempotency lives in the ingest layer
(dedupe on ``(tenant_id, provider_message_id)``), so re-running the
button never creates duplicate interactions.

Auth: tenant-scoped.  Accepts the tenant API key via
``Authorization: Bearer`` (or a session JWT) like the rest of the v1 API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import EmailBackfillJob, Integration, Tenant

router = APIRouter()

# Cap the window. The Gmail query uses ``newer_than:Nd``; 90 days is the
# product default and the ceiling — a larger window is both a quota and a
# cost concern (every new message fans out to the analysis pipeline).
_MAX_WINDOW_DAYS = 90

# Providers we can actually backfill today. Graph (Outlook) is a 501 until
# the mirror lands — see the design note in the task spec.
_SUPPORTED_PROVIDERS = {"google"}


class BackfillRequest(BaseModel):
    provider: str = "google"
    days: int = Field(default=_MAX_WINDOW_DAYS, ge=1)


class BackfillStartResponse(BaseModel):
    job_id: str
    status: str
    window_days: int


class BackfillStatusResponse(BaseModel):
    job_id: str
    provider: str
    status: str
    window_days: int
    fetched: int
    ingested: int
    skipped: int
    error: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


@router.post(
    "/email/backfill",
    status_code=202,
    response_model=BackfillStartResponse,
)
async def start_email_backfill(
    body: BackfillRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> BackfillStartResponse:
    """Queue a historical backfill for the tenant's connected mailbox."""
    provider = (body.provider or "google").strip().lower()
    # Clamp to [1, 90]; pydantic already enforced >= 1.
    window_days = min(int(body.days or _MAX_WINDOW_DAYS), _MAX_WINDOW_DAYS)

    integration = (
        await db.execute(
            select(Integration).where(
                Integration.tenant_id == tenant.id,
                Integration.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise HTTPException(
            status_code=404,
            detail=f"No connected '{provider}' mailbox for this tenant",
        )

    if provider not in _SUPPORTED_PROVIDERS:
        # Microsoft Graph (Outlook) is connected but backfill isn't wired yet.
        raise HTTPException(
            status_code=501,
            detail=f"Historical backfill for '{provider}' is not yet supported",
        )

    # One in-flight job per tenant+provider. Return the existing one rather
    # than fanning out duplicate imports against the same mailbox.
    existing = (
        await db.execute(
            select(EmailBackfillJob)
            .where(
                EmailBackfillJob.tenant_id == tenant.id,
                EmailBackfillJob.provider == provider,
                EmailBackfillJob.status.in_(("queued", "running")),
            )
            .order_by(EmailBackfillJob.created_at.desc())
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "A backfill is already running for this mailbox",
                "job_id": str(existing.id),
                "status": existing.status,
            },
        )

    job = EmailBackfillJob(
        tenant_id=tenant.id,
        integration_id=integration.id,
        provider=provider,
        status="queued",
        window_days=window_days,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Import here to avoid a circular import at module load (tasks pulls in
    # most of the service layer). Mirrors the email-push endpoint.
    from backend.app.tasks import email_backfill_run

    email_backfill_run.delay(str(job.id))

    return BackfillStartResponse(
        job_id=str(job.id),
        status=job.status,
        window_days=job.window_days,
    )


@router.get(
    "/email/backfill/{job_id}",
    response_model=BackfillStatusResponse,
)
async def get_email_backfill(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> BackfillStatusResponse:
    """Poll a backfill job's status + counters (tenant-scoped)."""
    job = (
        await db.execute(
            select(EmailBackfillJob).where(
                EmailBackfillJob.id == job_id,
                EmailBackfillJob.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Backfill job not found")

    return BackfillStatusResponse(
        job_id=str(job.id),
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
