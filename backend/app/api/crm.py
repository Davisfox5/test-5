"""CRM sync API — trigger on-demand pulls and inspect recent runs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import CrmSyncLog, Tenant
from backend.app.services.crm.sync_service import (
    SUPPORTED_PROVIDERS,
    sync_crm_for_tenant,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class CrmSyncSummaryOut(BaseModel):
    provider: str
    status: str
    customers_upserted: int
    contacts_upserted: int
    briefs_rebuilt: int
    error: Optional[str] = None


class CrmSyncLogOut(BaseModel):
    id: uuid.UUID
    provider: str
    status: str
    customers_upserted: int
    contacts_upserted: int
    briefs_rebuilt: int
    error: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]

    model_config = {"from_attributes": True}


@router.post("/crm/sync/{provider}", response_model=CrmSyncSummaryOut)
async def trigger_crm_sync(
    provider: str,
    sync: bool = True,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Pull customers + contacts from the CRM into LINDA.

    ``sync=true`` (default) runs inline and returns the summary once done.
    ``sync=false`` enqueues the Celery task and returns immediately —
    use for long-running syncs where you don't want the HTTP connection
    held open.

    On success, new customers get a debounced CustomerBriefBuilder rebuild
    so LINDA has a day-one dossier before the first call.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported CRM provider: {provider}",
        )

    if sync:
        summary = await sync_crm_for_tenant(db, tenant.id, provider)
        if summary.status == "failed" and summary.error and "not implemented" in (summary.error or "").lower():
            raise HTTPException(status_code=501, detail=summary.error)
        return summary

    # Enqueue the Celery fan-out.
    try:
        from backend.app.tasks import crm_sync_tenant

        crm_sync_tenant.delay(str(tenant.id), provider)
    except Exception:
        logger.exception("Failed to enqueue CRM sync task")
    return CrmSyncSummaryOut(
        provider=provider,
        status="scheduled",
        customers_upserted=0,
        contacts_upserted=0,
        briefs_rebuilt=0,
    )


@router.get("/crm/sync/logs", response_model=List[CrmSyncLogOut])
async def list_crm_sync_logs(
    provider: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Recent sync runs for this tenant, newest first."""
    stmt = (
        select(CrmSyncLog)
        .where(CrmSyncLog.tenant_id == tenant.id)
        .order_by(desc(CrmSyncLog.started_at))
        .limit(min(max(limit, 1), 100))
    )
    if provider:
        stmt = stmt.where(CrmSyncLog.provider == provider)
    return list((await db.execute(stmt)).scalars().all())
