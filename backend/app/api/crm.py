"""CRM sync API — trigger on-demand pulls, inspect recent runs, receive
provider webhooks for real-time sync."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import CrmSyncLog, Integration, Tenant
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


# ── Pipedrive webhooks ────────────────────────────────────────────────


@router.post("/crm/webhooks/pipedrive/{tenant_id}", status_code=202)
async def pipedrive_webhook(
    tenant_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_webhook_secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret"),
):
    """Receive a Pipedrive webhook for a tenant and schedule a targeted
    sync. Pipedrive signs events by letting you configure HTTP basic
    auth or a custom header on the webhook definition; we use the
    custom header path because it doesn't require us to expose a
    username/password combination in their UI.

    Events land in ``Integration.provider_config['webhook_secret']`` —
    set when the admin registers the webhook. Unrecognized tenants
    return 404; mismatched secrets return 403. Successful events are
    acknowledged with 202 and dispatched to the sync task, so
    Pipedrive doesn't retry on slow syncs.
    """
    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "pipedrive",
        )
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        raise HTTPException(status_code=404, detail="No Pipedrive integration for tenant")

    cfg = integ.provider_config or {}
    expected = cfg.get("webhook_secret")
    if expected and not (
        x_webhook_secret and hmac.compare_digest(str(expected), str(x_webhook_secret))
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    try:
        payload: Dict[str, Any] = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Pipedrive shape: ``{event: "added.deal"|"updated.person"|…,
    # current: {...row}, previous: {...}}``. We don't try to apply the
    # change in-process — we queue a full provider sync (customers +
    # contacts + deals) so the tenant always sees a consistent state.
    event = str(payload.get("event") or "")
    try:
        from backend.app.tasks import crm_sync_tenant

        crm_sync_tenant.delay(str(tenant_id), "pipedrive")
    except Exception:
        logger.exception(
            "Pipedrive webhook dispatch failed (tenant=%s event=%s)", tenant_id, event
        )
        # Still acknowledge — retrying doesn't help if our broker is down.
    return {"status": "accepted", "event": event}
