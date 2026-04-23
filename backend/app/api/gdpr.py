"""GDPR data-subject endpoints.

Two endpoints, admin-only, per tenant:

* ``GET /tenants/{tenant_id}/export`` — streams a line-delimited JSON
  archive of every row the tenant owns. Suitable for Articles 15
  (right of access) and 20 (data portability).
* ``DELETE /tenants/{tenant_id}`` — hard delete, Article 17. Scrubs
  every tenant-owned row and the tenant itself. Requires a
  ``reason`` in the body so the audit log has context.

Both endpoints require the caller to be an admin of the target
tenant (admin of tenant A cannot delete tenant B). The audit row is
written *before* the operation starts, so even a mid-operation
crash leaves a trace.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, get_current_principal
from backend.app.db import get_db
from backend.app.models import TenantDataOpsLog
from backend.app.services.tenant_dataops import export_tenant, hard_delete_tenant

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_tenant_admin(principal: AuthPrincipal, tenant_id: uuid.UUID) -> None:
    if principal.tenant.id != tenant_id:
        raise HTTPException(
            status_code=404, detail="Tenant not found for this principal"
        )
    if principal.role != "admin":
        raise HTTPException(
            status_code=403, detail="Admin role required for data-ops endpoints"
        )


class HardDeleteIn(BaseModel):
    reason: str = Field(..., min_length=10, max_length=500)
    confirm_tenant_name: str = Field(
        ...,
        description=(
            "Tenant name as typed by the admin in the confirmation prompt — "
            "mitigates accidental deletes. Must exactly match tenants.name."
        ),
    )


@router.get("/tenants/{tenant_id}/export")
async def export_tenant_data(
    tenant_id: uuid.UUID,
    reason: Optional[str] = Query(None, max_length=500),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Stream a GDPR data export for this tenant.

    Response is ``application/x-ndjson`` (one JSON doc per line) so
    multi-GB tenants don't need the server to buffer the whole bundle.
    The first line carries an ``_meta`` header, the last line carries
    an ``_eof`` marker with per-table counts.
    """
    _require_tenant_admin(principal, tenant_id)

    log_entry = TenantDataOpsLog(
        tenant_id=tenant_id,
        actor_user_id=principal.user_id,
        actor_email=principal.user.email if principal.user else None,
        operation="export",
        status="running",
        reason=reason,
    )
    db.add(log_entry)
    await db.flush()

    async def _stream():
        row_count = 0
        try:
            async for chunk in export_tenant(db, tenant_id):
                row_count += 1
                yield chunk
        except Exception as exc:
            log_entry.status = "failed"
            log_entry.error = str(exc)[:500]
            log_entry.finished_at = datetime.now(timezone.utc)
            await db.commit()
            raise
        else:
            log_entry.status = "success"
            log_entry.counts = {"lines": row_count}
            log_entry.finished_at = datetime.now(timezone.utc)
            await db.commit()

    filename = f"linda-export-{tenant_id}-{datetime.utcnow().strftime('%Y%m%d')}.ndjson"
    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Linda-Export-TenantId": str(tenant_id),
        },
    )


@router.delete("/tenants/{tenant_id}", status_code=200)
async def hard_delete_tenant_endpoint(
    tenant_id: uuid.UUID,
    body: HardDeleteIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Permanently delete a tenant. No undo.

    Safety gates:

    1. Caller must be admin of the tenant (enforced at the top).
    2. ``confirm_tenant_name`` in the body must match the tenant's
       actual name — mitigates the "I meant to delete tenant B"
       accident.
    3. ``reason`` is required (10 chars min) and persists on the audit
       log; this makes DPA reviews legible.
    """
    _require_tenant_admin(principal, tenant_id)
    if body.confirm_tenant_name != principal.tenant.name:
        raise HTTPException(
            status_code=400,
            detail=(
                "confirm_tenant_name does not match. Type the tenant name "
                "exactly to proceed."
            ),
        )

    log_entry = TenantDataOpsLog(
        tenant_id=tenant_id,
        actor_user_id=principal.user_id,
        actor_email=principal.user.email if principal.user else None,
        operation="delete",
        status="running",
        reason=body.reason,
    )
    db.add(log_entry)
    await db.flush()

    try:
        summary = await hard_delete_tenant(db, tenant_id)
        log_entry.status = "success"
        log_entry.counts = summary["deleted"]
        log_entry.finished_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:
        log_entry.status = "failed"
        log_entry.error = str(exc)[:500]
        log_entry.finished_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Hard delete failed: {exc}")

    return summary
