"""Action Items API — standalone endpoint for managing action items across interactions."""

import uuid
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
    require_scope,
)
from backend.app.db import get_db
from backend.app.models import ActionItem, Tenant
from backend.app.services import feedback_service
from backend.app.services.audit_log import audit_log

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
    # 'open' | 'done' | 'dismissed'. Snooze is orthogonal — see ``snoozed_until``.
    status: str
    due_date: Optional[date]
    calendar_event_id: Optional[str]
    email_draft: Optional[dict]
    call_script: Optional[list] = None
    next_step_type: Optional[str] = None
    recommended_channel: Optional[str] = None
    channel_reasoning: Optional[str] = None
    participants: list = Field(default_factory=list)
    prep_artifacts: list = Field(default_factory=list)
    parent_action_item_id: Optional[uuid.UUID] = None
    implicit_signal: Optional[str] = None
    manually_created: bool = False
    feedback_score: int = 0
    automation_status: str
    dismiss_reason: Optional[str] = None
    snoozed_until: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None
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
    dismiss_reason: Optional[str] = None
    snoozed_until: Optional[datetime] = None
    call_script: Optional[list] = None
    email_draft: Optional[dict] = None
    next_step_type: Optional[str] = None
    recommended_channel: Optional[str] = None
    channel_reasoning: Optional[str] = None
    participants: Optional[list] = None
    prep_artifacts: Optional[list] = None
    user_id: Optional[uuid.UUID] = None  # who is doing the edit (for feedback attribution)


class ActionItemCreate(BaseModel):
    """Manual creation by a rep / manager. Most fields optional —
    only ``title`` and ``interaction_id`` are required."""
    interaction_id: uuid.UUID
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    priority: str = "medium"
    due_date: Optional[date] = None
    assigned_to: Optional[uuid.UUID] = None
    next_step_type: Optional[str] = None
    recommended_channel: Optional[str] = None
    channel_reasoning: Optional[str] = None
    participants: list = Field(default_factory=list)
    prep_artifacts: list = Field(default_factory=list)
    email_draft: Optional[dict] = None
    call_script: Optional[list] = None
    parent_action_item_id: Optional[uuid.UUID] = None


class ActionItemBulkUpdate(BaseModel):
    ids: List[uuid.UUID]
    status: Optional[str] = None
    assigned_to: Optional[uuid.UUID] = None
    priority: Optional[str] = None
    snoozed_until: Optional[datetime] = None
    user_id: Optional[uuid.UUID] = None


class ActionItemFeedback(BaseModel):
    """Lightweight 'this was/wasn't useful' signal — feeds the learning loop."""
    helpful: bool
    note: Optional[str] = None
    user_id: Optional[uuid.UUID] = None


# Maps a status transition to the feedback event_type the model should learn from.
_STATUS_EVENT_MAP = {
    "done": "action_accepted",
    "dismissed": "action_dismissed",
    "open": "action_reopened",
}


# Phase 5B simplification: status enum is exactly {open, done, dismissed}.
# Snooze is orthogonal via ``snoozed_until`` — clients filter by the
# ``snoozed`` bucket via the dedicated query parameter, not via status.
_VALID_STATUSES = frozenset({"open", "done", "dismissed"})


def _normalize_status(value: Optional[str]) -> Optional[str]:
    """Map any incoming status string to the canonical set, or None.

    Tolerates legacy spellings (pending/in_progress/completed/rejected)
    so older clients during migration don't 422 — but every legacy
    spelling normalizes to one of {open, done, dismissed}.
    """
    if not value:
        return None
    v = value.lower().strip()
    if v in _VALID_STATUSES:
        return v
    if v in {"pending", "in_progress", "snoozed"}:
        return "open"
    if v == "completed":
        return "done"
    if v == "rejected":
        return "dismissed"
    return None  # unrecognized — caller decides whether to 422


def _expand_status_filter(value: str) -> list[str]:
    """Return the list of underlying ActionItem.status values to query for.

    With the simplified enum each canonical status maps to itself.
    Legacy aliases are normalized first so old client filters still work.
    """
    canonical = _normalize_status(value)
    if canonical:
        return [canonical]
    return [value.lower()]


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
        candidates = _expand_status_filter(status)
        if len(candidates) == 1:
            stmt = stmt.where(ActionItem.status == candidates[0])
        else:
            stmt = stmt.where(ActionItem.status.in_(candidates))
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


@router.patch(
    "/action-items/{action_item_id}",
    response_model=ActionItemOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def update_action_item(
    action_item_id: uuid.UUID,
    body: ActionItemUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
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
        normalized = _normalize_status(body.status)
        if normalized is None:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status: {body.status!r}. Expected one of {sorted(_VALID_STATUSES)}.",
            )
        item.status = normalized
        # Auto-stamp lifecycle transitions. Timestamps are sticky once set
        # (no clear-on-uncheck) so the audit trail survives a re-open.
        now = datetime.now(timezone.utc)
        if normalized == "done" and item.completed_at is None:
            item.completed_at = now
        if normalized == "dismissed" and item.dismissed_at is None:
            item.dismissed_at = now
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
    if body.dismiss_reason is not None:
        item.dismiss_reason = body.dismiss_reason
    if body.snoozed_until is not None:
        item.snoozed_until = body.snoozed_until
    if body.call_script is not None:
        item.call_script = body.call_script
    if body.email_draft is not None:
        item.email_draft = body.email_draft
    if body.next_step_type is not None:
        item.next_step_type = body.next_step_type
    if body.recommended_channel is not None:
        item.recommended_channel = body.recommended_channel
    if body.channel_reasoning is not None:
        item.channel_reasoning = body.channel_reasoning
    if body.participants is not None:
        item.participants = body.participants
    if body.prep_artifacts is not None:
        item.prep_artifacts = body.prep_artifacts

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

    # Phase 0 telemetry: record action-item lifecycle transitions as
    # intervention events for outcome bias correction. Imported lazily
    # so test environments that stub the service don't have to.
    if body.status is not None and body.status != old_status:
        try:
            from backend.app.services.intervention_events import (
                record_action_item_lifecycle,
            )
            await record_action_item_lifecycle(
                db,
                tenant_id=tenant.id,
                interaction_id=item.interaction_id,
                action_item_id=item.id,
                old_status=old_status,
                new_status=body.status,
                actor_user_id=body.user_id,
                dismiss_reason=body.dismiss_reason,
            )
        except Exception:  # noqa: BLE001 — telemetry must never fail the request
            pass

    await audit_log(
        db,
        principal,
        action="action_item.updated",
        resource_type="action_item",
        resource_id=str(item.id),
        before={
            "status": old_status,
            "automation_status": old_automation,
            "title": old_title,
            "description": old_description,
        },
        after={
            "status": item.status,
            "automation_status": item.automation_status,
            "title": item.title,
            "description": item.description,
        },
    )

    return item


# ── Manual creation ─────────────────────────────────────────────────────


@router.post(
    "/action-items",
    response_model=ActionItemOut,
    status_code=201,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def create_action_item(
    body: ActionItemCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Manually add an action item the system didn't generate.

    Use case: a rep notices something during review that the LLM missed
    and wants it tracked. Distinguished from LLM-generated items by
    ``manually_created=True`` so the learning loop doesn't treat it as
    confirmation of an LLM suggestion.
    """
    item = ActionItem(
        interaction_id=body.interaction_id,
        tenant_id=tenant.id,
        title=body.title,
        description=body.description,
        category=body.category,
        priority=body.priority,
        status="open",
        due_date=body.due_date,
        assigned_to=body.assigned_to,
        next_step_type=body.next_step_type,
        recommended_channel=body.recommended_channel,
        channel_reasoning=body.channel_reasoning,
        participants=body.participants,
        prep_artifacts=body.prep_artifacts,
        email_draft=body.email_draft,
        call_script=body.call_script,
        parent_action_item_id=body.parent_action_item_id,
        manually_created=True,
    )
    db.add(item)
    await db.flush()

    await audit_log(
        db,
        principal,
        action="action_item.created_manually",
        resource_type="action_item",
        resource_id=str(item.id),
        after={"title": item.title, "category": item.category, "priority": item.priority},
    )
    return item


# ── Bulk operations ─────────────────────────────────────────────────────


@router.patch(
    "/action-items/bulk",
    response_model=Dict[str, int],
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def bulk_update_action_items(
    body: ActionItemBulkUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Apply the same update to many action items.

    Returns ``{"updated": N}``. Status normalization is applied per-item;
    invalid statuses 422 the entire batch (no partial success — that's a
    foot-gun in bulk flows).
    """
    if not body.ids:
        return {"updated": 0}

    if body.status is not None:
        normalized = _normalize_status(body.status)
        if normalized is None:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status: {body.status!r}. Expected one of {sorted(_VALID_STATUSES)}.",
            )
    else:
        normalized = None

    stmt = select(ActionItem).where(
        ActionItem.id.in_(body.ids),
        ActionItem.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    items = list(result.scalars())

    now = datetime.now(timezone.utc)
    for item in items:
        if normalized is not None:
            item.status = normalized
            if normalized == "done" and item.completed_at is None:
                item.completed_at = now
            if normalized == "dismissed" and item.dismissed_at is None:
                item.dismissed_at = now
        if body.assigned_to is not None:
            item.assigned_to = body.assigned_to
        if body.priority is not None:
            item.priority = body.priority
        if body.snoozed_until is not None:
            item.snoozed_until = body.snoozed_until

    if items and normalized is not None:
        try:
            from backend.app.services.intervention_events import (
                record_action_item_lifecycle,
            )
            for item in items:
                await record_action_item_lifecycle(
                    db,
                    tenant_id=tenant.id,
                    interaction_id=item.interaction_id,
                    action_item_id=item.id,
                    old_status=None,
                    new_status=normalized,
                    actor_user_id=body.user_id,
                )
        except Exception:  # noqa: BLE001
            pass

    await audit_log(
        db,
        principal,
        action="action_item.bulk_updated",
        resource_type="action_item",
        resource_id=",".join(str(i.id) for i in items[:10]),
        after={"count": len(items), "status": normalized},
    )
    return {"updated": len(items)}


# ── Feedback (was/wasn't useful) ────────────────────────────────────────


@router.post(
    "/action-items/{action_item_id}/feedback",
    response_model=ActionItemOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def submit_action_item_feedback(
    action_item_id: uuid.UUID,
    body: ActionItemFeedback,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Mark an action item as helpful or not.

    Drives the learning loop: as ``feedback_score`` distributions
    accumulate per category / next_step_type / model version, future
    action item generation can suppress patterns that consistently get
    'not useful'. Helpful = +1, not helpful = -1.
    """
    stmt = select(ActionItem).where(
        ActionItem.id == action_item_id,
        ActionItem.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")

    item.feedback_score = (item.feedback_score or 0) + (1 if body.helpful else -1)

    feedback_service.emit_event(
        tenant_id=tenant.id,
        surface="analysis",
        event_type="action_helpful" if body.helpful else "action_not_helpful",
        signal_type="explicit",
        interaction_id=item.interaction_id,
        action_item_id=item.id,
        user_id=body.user_id,
        insight_dimension="action_items",
        payload={"note": body.note} if body.note else {},
    )

    await audit_log(
        db,
        principal,
        action="action_item.feedback",
        resource_type="action_item",
        resource_id=str(item.id),
        after={"helpful": body.helpful, "feedback_score": item.feedback_score},
    )
    return item
