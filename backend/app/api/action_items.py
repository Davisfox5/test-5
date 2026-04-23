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
from backend.app.services import feedback_service

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
    title: Optional[str] = None
    description: Optional[str] = None
    automation_status: Optional[str] = None
    user_id: Optional[uuid.UUID] = None  # who is doing the edit (for feedback attribution)


# Maps a status transition to the feedback event_type the model should learn from.
_STATUS_EVENT_MAP = {
    "done": "action_accepted",
    "completed": "action_accepted",
    "dismissed": "action_dismissed",
    "rejected": "action_dismissed",
}


def _emit_lifecycle_event(
    item: ActionItem,
    *,
    tenant_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    old_status: str,
    new_status: Optional[str],
    old_automation: str,
    new_automation: Optional[str],
    title_diff: Optional[dict],
    description_diff: Optional[dict],
) -> None:
    """Push action-item edit/lifecycle events to the feedback stream.

    Multiple events can fire for one PATCH (e.g. a user simultaneously
    edits the title AND marks it done — that's two distinct signals).
    """
    if title_diff is not None or description_diff is not None:
        feedback_service.emit_event(
            tenant_id=tenant_id,
            surface="analysis",
            event_type="action_edited",
            signal_type="implicit",
            interaction_id=item.interaction_id,
            action_item_id=item.id,
            user_id=user_id,
            insight_dimension="action_items",
            payload={
                "title_diff": title_diff,
                "description_diff": description_diff,
            },
        )

    if new_status and new_status != old_status:
        ev = _STATUS_EVENT_MAP.get(new_status.lower())
        if ev:
            feedback_service.emit_event(
                tenant_id=tenant_id,
                surface="analysis",
                event_type=ev,
                signal_type="implicit",
                interaction_id=item.interaction_id,
                action_item_id=item.id,
                user_id=user_id,
                insight_dimension="action_items",
                payload={"old_status": old_status, "new_status": new_status},
            )

    if (
        new_automation
        and new_automation != old_automation
        and new_automation == "auto_sent"
    ):
        feedback_service.emit_event(
            tenant_id=tenant_id,
            surface="analysis",
            event_type="action_auto_sent",
            signal_type="implicit",
            interaction_id=item.interaction_id,
            action_item_id=item.id,
            user_id=user_id,
            insight_dimension="action_items",
            payload={"old_automation": old_automation, "new_automation": new_automation},
        )


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

    old_status = item.status
    old_automation = item.automation_status
    old_title = item.title
    old_description = item.description

    if body.status is not None:
        item.status = body.status
    if body.assigned_to is not None:
        item.assigned_to = body.assigned_to
    if body.priority is not None:
        item.priority = body.priority
    if body.due_date is not None:
        item.due_date = body.due_date
    if body.title is not None:
        item.title = body.title
    if body.description is not None:
        item.description = body.description
    if body.automation_status is not None:
        item.automation_status = body.automation_status

    title_diff = (
        feedback_service.diff_summary(old_title or "", item.title or "")
        if body.title is not None and body.title != old_title
        else None
    )
    description_diff = (
        feedback_service.diff_summary(old_description or "", item.description or "")
        if body.description is not None and body.description != (old_description or "")
        else None
    )

    _emit_lifecycle_event(
        item,
        tenant_id=tenant.id,
        user_id=body.user_id,
        old_status=old_status,
        new_status=body.status,
        old_automation=old_automation,
        new_automation=body.automation_status,
        title_diff=title_diff,
        description_diff=description_diff,
    )

    return item
