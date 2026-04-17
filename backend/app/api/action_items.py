"""Action Items API — standalone endpoint for managing action items across interactions."""

import uuid
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import ActionItem, Tenant

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class ActionItemOut(BaseModel):
    id: uuid.UUID
    interaction_id: uuid.UUID
    tenant_id: uuid.UUID
    assigned_to: Optional[uuid.UUID]
    title: str
    description: Optional[str]
    category: Optional[str]
    priority: str
    status: str
    due_date: Optional[date]
    calendar_event_id: Optional[str]
    email_draft: Optional[dict]
    automation_status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ActionItemUpdate(BaseModel):
    status: Optional[str] = None
    assigned_to: Optional[uuid.UUID] = None
    priority: Optional[str] = None
    due_date: Optional[date] = None


# ── Endpoints ────────────────────────────────────────────


@router.get("/action-items", response_model=List[ActionItemOut])
async def list_action_items(
    status: Optional[str] = Query(None, description="Filter by status"),
    priority: Optional[str] = Query(None, description="Filter by priority"),
    category: Optional[str] = Query(None, description="Filter by category"),
    assigned_to: Optional[uuid.UUID] = Query(None, description="Filter by assigned user"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(ActionItem)
        .where(ActionItem.tenant_id == tenant.id)
        .order_by(ActionItem.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(ActionItem.status == status)
    if priority is not None:
        stmt = stmt.where(ActionItem.priority == priority)
    if category is not None:
        stmt = stmt.where(ActionItem.category == category)
    if assigned_to is not None:
        stmt = stmt.where(ActionItem.assigned_to == assigned_to)

    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/action-items/{action_item_id}", response_model=ActionItemOut)
async def get_action_item(
    action_item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(ActionItem).where(
        ActionItem.id == action_item_id,
        ActionItem.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")
    return item


@router.patch("/action-items/{action_item_id}", response_model=ActionItemOut)
async def update_action_item(
    action_item_id: uuid.UUID,
    body: ActionItemUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(ActionItem).where(
        ActionItem.id == action_item_id,
        ActionItem.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")

    if body.status is not None:
        item.status = body.status
    if body.assigned_to is not None:
        item.assigned_to = body.assigned_to
    if body.priority is not None:
        item.priority = body.priority
    if body.due_date is not None:
        item.due_date = body.due_date

    return item
