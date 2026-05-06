"""Notifications API — per-user inbox + read tracking."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
    require_scope,
)
from backend.app.db import get_db
from backend.app.models import Notification, Tenant
from backend.app.services.notifications import (
    mark_all_read,
    mark_read,
)

router = APIRouter()


class NotificationOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    title: str
    body: Optional[str]
    link_url: Optional[str]
    action_item_id: Optional[uuid.UUID]
    interaction_id: Optional[uuid.UUID]
    is_read: bool
    read_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationList(BaseModel):
    items: List[NotificationOut]
    unread_count: int


@router.get(
    "/notifications",
    response_model=NotificationList,
    dependencies=[Depends(require_scope("notifications:read"))],
)
async def list_notifications(
    only_unread: bool = Query(False, description="Filter to unread only"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Return the current user's notifications, newest first."""
    if not principal.user:
        raise HTTPException(status_code=401, detail="Not a user")

    stmt = (
        select(Notification)
        .where(
            Notification.user_id == principal.user.id,
            Notification.tenant_id == tenant.id,
        )
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if only_unread:
        stmt = stmt.where(Notification.is_read.is_(False))

    items = list((await db.execute(stmt)).scalars())

    unread = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == principal.user.id,
            Notification.tenant_id == tenant.id,
            Notification.is_read.is_(False),
        )
    )
    unread_count = unread.scalar_one() or 0

    return NotificationList(items=items, unread_count=int(unread_count))


@router.get(
    "/notifications/unread-count",
    response_model=dict,
    dependencies=[Depends(require_scope("notifications:read"))],
)
async def unread_count(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Lightweight endpoint for the notification bell badge."""
    if not principal.user:
        return {"unread_count": 0}
    result = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == principal.user.id,
            Notification.tenant_id == tenant.id,
            Notification.is_read.is_(False),
        )
    )
    return {"unread_count": int(result.scalar_one() or 0)}


@router.post(
    "/notifications/{notification_id}/read",
    response_model=dict,
    dependencies=[Depends(require_scope("notifications:write"))],
)
async def mark_notification_read(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    if not principal.user:
        raise HTTPException(status_code=401, detail="Not a user")
    ok = await mark_read(db, notification_id=notification_id, user_id=principal.user.id)
    return {"ok": ok}


@router.post(
    "/notifications/mark-all-read",
    response_model=dict,
    dependencies=[Depends(require_scope("notifications:write"))],
)
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    if not principal.user:
        raise HTTPException(status_code=401, detail="Not a user")
    n = await mark_all_read(db, user_id=principal.user.id, tenant_id=tenant.id)
    return {"updated": n}
