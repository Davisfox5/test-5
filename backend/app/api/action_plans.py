"""Action Plans API — REST + SSE notification dispatch.

Surfaces:

* ``GET /action-plans`` — list plans for the tenant (filterable by
  status, interaction, customer).
* ``GET /action-plans/{plan_id}`` — full plan with steps + the latest
  artifact per step.
* ``POST /action-plans/{plan_id}/steps/{step_id}/complete`` — mark a
  step done; engine cascades to downstream.
* ``POST /action-plans/{plan_id}/steps/{step_id}/skip`` — skip with
  optional reason.
* ``DELETE /action-plans/{plan_id}/steps/{step_id}`` — soft-delete.
* ``POST /action-plans/{plan_id}/steps/{step_id}/notes`` — agent adds
  a freeform note; runs Call D extraction; auto-applies.
* ``POST /action-plans/{plan_id}/steps/{step_id}/override`` — agent
  manually edits extracted slot values; cascades regen.
* ``POST /action-plans/{plan_id}/steps/{step_id}/sent`` — records that
  an outbound email was sent so the matcher can tie an inbound reply
  back.
* ``POST /action-plans/{plan_id}/replan`` — agent edits goal or
  switches domain; re-runs synthesis preserving done steps.

The SSE channel reused is the existing ``/notifications/stream`` (per
the locked decision). We just push ``action_plan.*`` event payloads
into the same notification_channel.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
    require_scope,
)
from backend.app.db import get_db
from backend.app.models import (
    ActionPlan,
    ActionStep,
    Interaction,
    StepArtifact,
    StepResponse,
    Tenant,
)
from backend.app.services.action_plan.engine import (
    ActionPlanEngine,
    TERMINAL_STATES,
)
from backend.app.services.action_plan.extractor import ResponseExtractor
from backend.app.services.notifications import publish_notification

logger = logging.getLogger(__name__)

router = APIRouter()


# ──────────────────────────────────────────────────────────
# Pydantic shapes
# ──────────────────────────────────────────────────────────


class StepArtifactOut(BaseModel):
    id: uuid.UUID
    version: int
    kind: str
    payload: Dict[str, Any]
    model_tier: Optional[str]
    generated_at: datetime
    superseded_at: Optional[datetime]

    model_config = {"from_attributes": True}


class StepResponseOut(BaseModel):
    id: uuid.UUID
    source: str
    note_text: Optional[str]
    extracted_data: Dict[str, Any]
    unfilled_reasons: Dict[str, Any]
    extraction_confidence: Optional[float]
    source_quotes: Dict[str, Any]
    received_at: datetime
    agent_overridden: bool

    model_config = {"from_attributes": True}


class ActionStepOut(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    assigned_to: Optional[uuid.UUID]
    title: str
    description: Optional[str]
    intent: Optional[str]
    priority: str
    due_date: Optional[Any]
    recommended_channel: Optional[str]
    channel_reasoning: Optional[str]
    participants: List[Dict[str, Any]]
    prep_artifacts: List[Any]
    implicit_signal: Optional[str]
    state: str
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    skipped_at: Optional[datetime]
    deleted_at: Optional[datetime]
    depends_on: List[str]
    input_slots: List[Dict[str, Any]]
    output_schema: List[Dict[str, Any]]
    output_data: Dict[str, Any]
    kb_source: Optional[Dict[str, Any]]
    compliance_level: Optional[str]
    role_in_plan: str
    target_integration: Optional[str]
    integration_operation: Optional[str]
    artifact_version: int
    artifact_stale: bool
    regen_debounce_until: Optional[datetime]
    skip_reason: Optional[str]
    # True when the synthesizer judged the step's outbound action
    # (typically an email) requires a customer reply. Drives the
    # post-Send transition: True → awaiting_response, False → done.
    awaits_response: bool = False
    # Lazy-draft state: drafted | ready_to_draft | pending_upstream |
    # draft_blocked. The SPA renders different per-step UI per state.
    # Defaults to "drafted" for back-compat with plans built before
    # this column existed.
    draft_state: str = "drafted"
    created_at: datetime
    # Computed surfaces
    latest_artifact: Optional[StepArtifactOut] = None
    responses: List[StepResponseOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ActionPlanOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    interaction_id: Optional[uuid.UUID]
    customer_id: Optional[uuid.UUID]
    goal: Optional[str]
    domain: str
    status: str
    customer_endpoint_step_id: Optional[uuid.UUID]
    procedures_applied: List[Dict[str, Any]]
    external_context_snapshot: Dict[str, Any]
    version: int
    manually_created: bool
    created_at: datetime
    completed_at: Optional[datetime]
    steps: List[ActionStepOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ActionPlanList(BaseModel):
    items: List[ActionPlanOut]


class NoteCreate(BaseModel):
    note_text: str = Field(..., min_length=1, max_length=10_000)


class OverrideRequest(BaseModel):
    slot_key: str
    value: Any


class SentRequest(BaseModel):
    outbound_message_id: str = Field(..., min_length=1, max_length=512)


class SkipRequest(BaseModel):
    reason: Optional[str] = None


class CompleteRequest(BaseModel):
    output_data: Optional[Dict[str, Any]] = None


class ScheduleMeetingForStepRequest(BaseModel):
    """Optional overrides for the per-step meeting scheduler."""
    start: Optional[datetime] = None
    duration_minutes: int = 30
    location: Optional[str] = None
    override_subject: Optional[str] = None
    override_participants: Optional[list] = None
    conference_provider: Optional[str] = None


class ScheduleMeetingForStepResult(BaseModel):
    success: bool
    provider: str
    event_id: Optional[str] = None
    join_url: Optional[str] = None
    html_link: Optional[str] = None
    ics_payload: Optional[str] = None
    note: Optional[str] = None
    error: Optional[str] = None


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────


async def _load_plan_or_404(
    db: AsyncSession,
    tenant: Tenant,
    plan_id: uuid.UUID,
) -> ActionPlan:
    plan = await db.get(ActionPlan, plan_id)
    if plan is None or plan.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


async def _load_step_or_404(
    db: AsyncSession,
    tenant: Tenant,
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
) -> ActionStep:
    step = await db.get(ActionStep, step_id)
    if (
        step is None
        or step.tenant_id != tenant.id
        or step.plan_id != plan_id
    ):
        raise HTTPException(status_code=404, detail="Step not found")
    return step


def _build_step_outs(
    step_rows: List[ActionStep],
    latest_artifacts: Dict[uuid.UUID, StepArtifact],
    responses_by_step: Dict[uuid.UUID, List[StepResponse]],
) -> List[ActionStepOut]:
    """Pure projection from already-loaded rows to ``ActionStepOut`` list."""
    step_outs: List[ActionStepOut] = []
    for s in step_rows:
        artifact = latest_artifacts.get(s.id)
        step_outs.append(
            ActionStepOut(
                id=s.id,
                plan_id=s.plan_id,
                assigned_to=s.assigned_to,
                title=s.title,
                description=s.description,
                intent=s.intent,
                priority=s.priority,
                due_date=s.due_date,
                recommended_channel=s.recommended_channel,
                channel_reasoning=s.channel_reasoning,
                participants=s.participants or [],
                prep_artifacts=s.prep_artifacts or [],
                implicit_signal=s.implicit_signal,
                state=s.state,
                started_at=s.started_at,
                completed_at=s.completed_at,
                skipped_at=s.skipped_at,
                deleted_at=s.deleted_at,
                depends_on=list(s.depends_on or []),
                input_slots=list(s.input_slots or []),
                output_schema=list(s.output_schema or []),
                output_data=dict(s.output_data or {}),
                kb_source=s.kb_source,
                compliance_level=s.compliance_level,
                role_in_plan=s.role_in_plan,
                target_integration=s.target_integration,
                integration_operation=s.integration_operation,
                artifact_version=s.artifact_version,
                artifact_stale=s.artifact_stale,
                regen_debounce_until=s.regen_debounce_until,
                skip_reason=s.skip_reason,
                awaits_response=bool(getattr(s, "awaits_response", False)),
                draft_state=getattr(s, "draft_state", None) or "drafted",
                created_at=s.created_at,
                latest_artifact=(
                    StepArtifactOut.model_validate(artifact, from_attributes=True)
                    if artifact is not None else None
                ),
                responses=[
                    StepResponseOut.model_validate(r, from_attributes=True)
                    for r in responses_by_step.get(s.id, [])
                ],
            )
        )

    return step_outs


def _plan_out_from_rows(
    plan: ActionPlan, step_outs: List[ActionStepOut]
) -> ActionPlanOut:
    return ActionPlanOut(
        id=plan.id,
        tenant_id=plan.tenant_id,
        interaction_id=plan.interaction_id,
        customer_id=plan.customer_id,
        goal=plan.goal,
        domain=plan.domain,
        status=plan.status,
        customer_endpoint_step_id=plan.customer_endpoint_step_id,
        procedures_applied=list(plan.procedures_applied or []),
        external_context_snapshot=dict(plan.external_context_snapshot or {}),
        version=plan.version,
        manually_created=plan.manually_created,
        created_at=plan.created_at,
        completed_at=plan.completed_at,
        steps=step_outs,
    )


async def _build_plan_out(
    db: AsyncSession, plan: ActionPlan,
) -> ActionPlanOut:
    """Load steps + latest artifact + responses for one plan."""
    step_rows = list(
        (
            await db.execute(
                select(ActionStep)
                .where(ActionStep.plan_id == plan.id)
                .order_by(ActionStep.created_at)
            )
        ).scalars()
    )
    step_ids = [s.id for s in step_rows]

    latest_artifacts: Dict[uuid.UUID, StepArtifact] = {}
    if step_ids:
        artifact_rows = list(
            (
                await db.execute(
                    select(StepArtifact)
                    .where(StepArtifact.step_id.in_(step_ids))
                    .order_by(StepArtifact.step_id, StepArtifact.version.desc())
                )
            ).scalars()
        )
        for a in artifact_rows:
            if a.step_id not in latest_artifacts:
                latest_artifacts[a.step_id] = a

    responses_by_step: Dict[uuid.UUID, List[StepResponse]] = {}
    if step_ids:
        resp_rows = list(
            (
                await db.execute(
                    select(StepResponse)
                    .where(StepResponse.step_id.in_(step_ids))
                    .order_by(StepResponse.received_at)
                )
            ).scalars()
        )
        for r in resp_rows:
            responses_by_step.setdefault(r.step_id, []).append(r)

    step_outs = _build_step_outs(step_rows, latest_artifacts, responses_by_step)
    return _plan_out_from_rows(plan, step_outs)


async def _build_plan_outs_bulk(
    db: AsyncSession, plans: List[ActionPlan],
) -> List[ActionPlanOut]:
    """Same projection as ``_build_plan_out`` but for many plans at once
    using three batched IN-queries (steps, artifacts, responses) — avoids
    the N+1 storm a list endpoint would otherwise generate. With limit=50
    this collapses 151 queries into 4."""
    if not plans:
        return []
    plan_ids = [p.id for p in plans]

    all_steps = list(
        (
            await db.execute(
                select(ActionStep)
                .where(ActionStep.plan_id.in_(plan_ids))
                .order_by(ActionStep.created_at)
            )
        ).scalars()
    )
    steps_by_plan: Dict[uuid.UUID, List[ActionStep]] = {pid: [] for pid in plan_ids}
    for s in all_steps:
        steps_by_plan.setdefault(s.plan_id, []).append(s)

    step_ids = [s.id for s in all_steps]
    latest_artifacts: Dict[uuid.UUID, StepArtifact] = {}
    responses_by_step: Dict[uuid.UUID, List[StepResponse]] = {}
    if step_ids:
        artifact_rows = list(
            (
                await db.execute(
                    select(StepArtifact)
                    .where(StepArtifact.step_id.in_(step_ids))
                    .order_by(StepArtifact.step_id, StepArtifact.version.desc())
                )
            ).scalars()
        )
        for a in artifact_rows:
            if a.step_id not in latest_artifacts:
                latest_artifacts[a.step_id] = a
        resp_rows = list(
            (
                await db.execute(
                    select(StepResponse)
                    .where(StepResponse.step_id.in_(step_ids))
                    .order_by(StepResponse.received_at)
                )
            ).scalars()
        )
        for r in resp_rows:
            responses_by_step.setdefault(r.step_id, []).append(r)

    out: List[ActionPlanOut] = []
    for p in plans:
        step_outs = _build_step_outs(
            steps_by_plan.get(p.id, []), latest_artifacts, responses_by_step
        )
        out.append(_plan_out_from_rows(p, step_outs))
    return out


def _emit_event(
    *,
    tenant: Tenant,
    principal: AuthPrincipal,
    event: str,
    plan_id: uuid.UUID,
    step_id: Optional[uuid.UUID] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Push an action_plan.* event to the user's SSE channel."""
    if not principal.user:
        return
    payload = {
        "type": event,
        "plan_id": str(plan_id),
        "step_id": str(step_id) if step_id else None,
    }
    if extra:
        payload.update(extra)
    publish_notification(
        tenant_id=tenant.id,
        user_id=principal.user.id,
        payload=payload,
    )


async def _emit_plan_updated_webhook(
    db: AsyncSession,
    tenant: Tenant,
    plan: ActionPlan,
    *,
    reason: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Outbound ``action_plan.updated`` webhook — the external analogue of
    the SSE events above, for consumers (Flex console) mirroring plans.
    Fired on material plan mutations (step edit/delete); creation and
    step completion keep their dedicated events."""
    try:
        from backend.app.services.webhook_dispatcher import emit_event

        payload: Dict[str, Any] = {
            "plan_id": str(plan.id),
            "customer_id": str(plan.customer_id) if plan.customer_id else None,
            "interaction_id": str(plan.interaction_id) if plan.interaction_id else None,
            "goal": plan.goal,
            "status": plan.status,
            "version": plan.version,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        await emit_event(db, tenant.id, "action_plan.updated", payload)
    except Exception:
        logger.exception("emit action_plan.updated webhook failed for %s", plan.id)


# ──────────────────────────────────────────────────────────
# Read endpoints
# ──────────────────────────────────────────────────────────


@router.get(
    "/action-plans",
    response_model=ActionPlanList,
    dependencies=[Depends(require_scope("action_items:read"))],
)
async def list_plans(
    status: Optional[str] = Query(None, description="active|completed|abandoned|draft"),
    interaction_id: Optional[uuid.UUID] = None,
    customer_id: Optional[uuid.UUID] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    conditions = [ActionPlan.tenant_id == tenant.id]
    if status:
        conditions.append(ActionPlan.status == status)
    if interaction_id:
        conditions.append(ActionPlan.interaction_id == interaction_id)
    if customer_id:
        conditions.append(ActionPlan.customer_id == customer_id)

    rows = await db.execute(
        select(ActionPlan)
        .where(and_(*conditions))
        .order_by(ActionPlan.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    plans = list(rows.scalars())
    items = await _build_plan_outs_bulk(db, plans)
    return ActionPlanList(items=items)


@router.get(
    "/action-plans/{plan_id}",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:read"))],
)
async def get_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    plan = await _load_plan_or_404(db, tenant, plan_id)
    return await _build_plan_out(db, plan)


# ──────────────────────────────────────────────────────────
# Manual creation
# ──────────────────────────────────────────────────────────


class PlanStepIn(BaseModel):
    """Initial step shape when creating a plan from scratch.

    Only ``title`` is required — everything else has sensible defaults
    on ``ActionStep``. ``due_date`` accepts ISO date strings; ``priority``
    accepts ``low|medium|high``.
    """

    title: str = Field(..., min_length=1, max_length=300)
    description: Optional[str] = None
    intent: Optional[str] = None
    priority: Optional[str] = Field(default="medium")
    due_date: Optional[str] = None  # ISO date YYYY-MM-DD
    recommended_channel: Optional[str] = None
    assigned_to: Optional[uuid.UUID] = None


class ActionPlanCreate(BaseModel):
    """POST body for manually creating a plan.

    Plans that come out of the analysis pipeline have an
    ``interaction_id`` and a synthesised step graph; this endpoint is for
    plans that don't (Linda chat, reseller-built UIs, admin dashboards).
    ``customer_id`` is optional — a plan can be a generic to-do list with
    no specific customer attached.
    """

    goal: str = Field(..., min_length=1, max_length=1000)
    customer_id: Optional[uuid.UUID] = None
    domain: Optional[str] = Field(
        default=None,
        description=(
            "sales | customer_service | it_support | generic. "
            "Defaults to the tenant's default_domain."
        ),
    )
    status: Optional[str] = Field(
        default="active",
        description="draft | active | completed | abandoned",
    )
    steps: List[PlanStepIn] = Field(default_factory=list)


@router.post(
    "/action-plans",
    response_model=ActionPlanOut,
    status_code=201,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def create_plan(
    body: ActionPlanCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Create an Action Plan from scratch (no interaction required).

    Plans created via this endpoint are stamped ``manually_created=True``
    so the synthesis pipeline knows not to overwrite them on the next
    interaction-driven re-plan. Returns the freshly-built plan with any
    inline steps attached.
    """
    from datetime import date as _date

    valid_domains = {"sales", "customer_service", "it_support", "generic"}
    valid_statuses = {"draft", "active", "completed", "abandoned"}

    domain = (body.domain or tenant.default_domain or "generic").strip()
    if domain not in valid_domains:
        raise HTTPException(
            status_code=422,
            detail=f"domain must be one of {sorted(valid_domains)}",
        )
    status = (body.status or "active").strip()
    if status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(valid_statuses)}",
        )
    if body.customer_id is not None:
        # Tenant-scope check so a caller can't attach a plan to another
        # tenant's customer by guessing the id.
        from backend.app.models import Customer

        customer = await db.get(Customer, body.customer_id)
        if customer is None or customer.tenant_id != tenant.id:
            raise HTTPException(status_code=404, detail="Customer not found")

    plan = ActionPlan(
        tenant_id=tenant.id,
        interaction_id=None,
        customer_id=body.customer_id,
        goal=body.goal.strip(),
        domain=domain,
        status=status,
        manually_created=True,
    )
    db.add(plan)
    await db.flush()

    for step_body in body.steps:
        priority = (step_body.priority or "medium").strip()
        if priority not in {"low", "medium", "high"}:
            priority = "medium"
        due: Optional[_date] = None
        if step_body.due_date:
            try:
                due = _date.fromisoformat(step_body.due_date)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"step.due_date must be ISO YYYY-MM-DD (got {step_body.due_date!r})",
                )
        step = ActionStep(
            tenant_id=tenant.id,
            plan_id=plan.id,
            title=step_body.title.strip()[:300],
            description=step_body.description,
            intent=step_body.intent,
            priority=priority,
            due_date=due,
            recommended_channel=step_body.recommended_channel,
            assigned_to=step_body.assigned_to,
        )
        db.add(step)
    await db.flush()

    # Emit a webhook event so subscribers can react to manually-created
    # plans the same way they react to pipeline-generated ones.
    try:
        from backend.app.services.webhook_dispatcher import emit_event

        await emit_event(
            db,
            tenant.id,
            "action_plan.created",
            {
                "plan_id": str(plan.id),
                "customer_id": str(plan.customer_id) if plan.customer_id else None,
                "goal": plan.goal,
                "domain": plan.domain,
                "status": plan.status,
                "manually_created": True,
                "step_count": len(body.steps),
            },
        )
    except Exception:
        logger.exception("emit action_plan.created webhook failed for %s", plan.id)

    _emit_event(
        tenant=tenant, principal=principal, event="action_plan.created",
        plan_id=plan.id,
    )

    return await _build_plan_out(db, plan)


# ──────────────────────────────────────────────────────────
# Step transitions
# ──────────────────────────────────────────────────────────


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/complete",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def complete_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: CompleteRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)
    engine = ActionPlanEngine()
    affected = await engine.complete_step(
        db, step=step, output_data=body.output_data, source="auto_mark_done",
    )
    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.state_changed",
        plan_id=plan_id, step_id=step_id,
        extra={"new_state": "done", "affected_step_ids": [str(s) for s in affected]},
    )
    try:
        from backend.app.services.webhook_dispatcher import emit_event

        await emit_event(
            db, tenant.id, "action_plan.step_completed",
            {
                "plan_id": str(plan_id),
                "step_id": str(step_id),
                "step_title": step.title,
                "affected_step_ids": [str(s) for s in affected],
            },
        )
        # Plan completion fires when this step *was* the customer endpoint
        # and the engine just moved the plan to status='completed'.
        if plan.status == "completed":
            await emit_event(
                db, tenant.id, "action_plan.completed",
                {
                    "plan_id": str(plan_id),
                    "customer_id": str(plan.customer_id) if plan.customer_id else None,
                    "goal": plan.goal,
                    "domain": plan.domain,
                },
            )
    except Exception:
        logger.exception("emit action_plan.step_completed webhook failed for %s", step_id)
    return await _build_plan_out(db, plan)


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/skip",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def skip_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: SkipRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)
    engine = ActionPlanEngine()
    affected = await engine.skip_step(db, step=step, reason=body.reason)
    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.state_changed",
        plan_id=plan_id, step_id=step_id,
        extra={"new_state": "skipped", "affected_step_ids": [str(s) for s in affected]},
    )
    try:
        from backend.app.services.webhook_dispatcher import emit_event

        await emit_event(
            db, tenant.id, "action_plan.step_skipped",
            {
                "plan_id": str(plan_id),
                "step_id": str(step_id),
                "step_title": step.title,
                "reason": body.reason,
                "affected_step_ids": [str(s) for s in affected],
            },
        )
    except Exception:
        logger.exception("emit action_plan.step_skipped webhook failed for %s", step_id)
    return await _build_plan_out(db, plan)


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/restore",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def restore_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Undo a skip. Step returns to ready/blocked based on dep state."""
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)
    engine = ActionPlanEngine()
    affected = await engine.restore_step(db, step=step)
    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.state_changed",
        plan_id=plan_id, step_id=step_id,
        extra={"new_state": "restored", "affected_step_ids": [str(s) for s in affected]},
    )
    return await _build_plan_out(db, plan)


class StepEditIn(BaseModel):
    """Inline edits the rep can make to a step. All fields optional; only
    those provided are updated. Editing a step also writes a row to
    ``StepFeedbackLog`` so the synthesizer can adapt this user's future
    plans toward their preferred phrasing/channel/priority.
    """
    title: Optional[str] = None
    description: Optional[str] = None
    intent: Optional[str] = None
    priority: Optional[str] = None  # 'high' | 'medium' | 'low'
    due_date: Optional[str] = None  # YYYY-MM-DD; pass '' to clear
    recommended_channel: Optional[str] = None
    channel_reasoning: Optional[str] = None
    awaits_response: Optional[bool] = None


@router.patch(
    "/action-plans/{plan_id}/steps/{step_id}",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def edit_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: StepEditIn,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Inline-edit a step. Writes a feedback log entry so the user's
    future plans get adapted toward this edit's shape."""
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    before = {
        "title": step.title,
        "description": step.description,
        "intent": step.intent,
        "priority": step.priority,
        "due_date": step.due_date.isoformat() if step.due_date else None,
        "recommended_channel": step.recommended_channel,
        "channel_reasoning": step.channel_reasoning,
        "awaits_response": getattr(step, "awaits_response", None),
    }
    updates = body.model_dump(exclude_unset=True)
    changed_keys = []
    for k, v in updates.items():
        if k == "due_date":
            from datetime import date as _date
            if v == "" or v is None:
                step.due_date = None
            else:
                try:
                    step.due_date = _date.fromisoformat(str(v))
                except ValueError:
                    raise HTTPException(400, "due_date must be YYYY-MM-DD or empty")
            changed_keys.append(k)
            continue
        if hasattr(step, k):
            setattr(step, k, v)
            changed_keys.append(k)

    after = {k: (step.due_date.isoformat() if k == "due_date" and step.due_date else getattr(step, k, None)) for k in before}

    # Feedback log: persist what changed, scoped to the editing user.
    if changed_keys and principal.user is not None:
        try:
            from backend.app.models import StepFeedbackLog
            log = StepFeedbackLog(
                tenant_id=tenant.id,
                user_id=principal.user.id,
                plan_id=plan_id,
                step_id=step_id,
                before=before,
                after=after,
                changed_keys=changed_keys,
            )
            db.add(log)
        except Exception:  # noqa: BLE001
            # Feedback logging is opportunistic. Never block an edit on
            # the log row failing — the synthesizer can fall back to the
            # canonical prompt if no feedback is available.
            import logging as _logging
            _logging.getLogger(__name__).exception("StepFeedbackLog insert failed")

    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.edited",
        plan_id=plan_id, step_id=step_id,
        extra={"changed_keys": changed_keys},
    )
    await _emit_plan_updated_webhook(
        db, tenant, plan, reason="step_edited",
        extra={"step_id": str(step_id), "changed_keys": changed_keys},
    )
    return await _build_plan_out(db, plan)


@router.delete(
    "/action-plans/{plan_id}/steps/{step_id}",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def delete_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)
    engine = ActionPlanEngine()
    affected = await engine.delete_step(db, step=step)
    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.state_changed",
        plan_id=plan_id, step_id=step_id,
        extra={"new_state": "deleted", "affected_step_ids": [str(s) for s in affected]},
    )
    await _emit_plan_updated_webhook(
        db, tenant, plan, reason="step_deleted", extra={"step_id": str(step_id)}
    )
    return await _build_plan_out(db, plan)


# ──────────────────────────────────────────────────────────
# Notes + overrides + outbound tagging
# ──────────────────────────────────────────────────────────


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/notes",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def add_note(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: NoteCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Agent adds a manual note. Runs Call D extraction; auto-applies."""
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)
    if step.state in TERMINAL_STATES and step.state != "done":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add a note to a {step.state} step",
        )

    extractor = ResponseExtractor()
    extraction = await extractor.extract_for_step(
        step=step,
        source_label="manual note",
        source_content=body.note_text,
    )

    response = StepResponse(
        step_id=step.id,
        tenant_id=tenant.id,
        source="manual_note",
        note_text=body.note_text,
        extracted_data=extraction.extracted,
        source_quotes=extraction.source_quotes,
        unfilled_reasons=extraction.unfilled_reasons,
        extraction_confidence=extraction.confidence,
    )
    db.add(response)
    await db.flush()

    engine = ActionPlanEngine()
    await engine.apply_response(db, step=step, response=response)
    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.response_received",
        plan_id=plan_id, step_id=step_id,
        extra={"source": "manual_note"},
    )
    return await _build_plan_out(db, plan)


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/override",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def override_slot(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: OverrideRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Agent edits an extracted slot value. Marks override + cascades.

    The agent override is the trust-recovery path for the auto-apply
    behavior — they can fix an extraction the AI got wrong, and the
    downstream artifact regenerates with the corrected value.
    """
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    merged = dict(step.output_data or {})
    merged[body.slot_key] = body.value
    step.output_data = merged

    # Audit: stamp the latest response (if any) as overridden.
    rows = await db.execute(
        select(StepResponse)
        .where(StepResponse.step_id == step.id)
        .order_by(StepResponse.received_at.desc())
        .limit(1)
    )
    latest = rows.scalar_one_or_none()
    if latest is not None:
        latest.agent_overridden = True

    engine = ActionPlanEngine()
    await engine._propagate_partial_fill(db, step=step)  # noqa: SLF001
    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.slot_overridden",
        plan_id=plan_id, step_id=step_id,
        extra={"slot_key": body.slot_key},
    )
    return await _build_plan_out(db, plan)


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/sent",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def record_sent(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: SentRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Record that an outbound email was sent for this step.

    The frontend calls this after a successful send through the
    existing /emails endpoint. Records the provider_message_id so the
    inbound matcher can tie a future reply back via RFC 822 headers.

    Next state depends on whether the synthesizer flagged this step
    as awaiting a reply:
      * ``step.awaits_response == True``  -> awaiting_response
      * ``step.awaits_response == False`` -> done (fire-and-forget)

    The synthesizer sets ``awaits_response`` per step based on whether
    the drafted body actually asks the customer for something back.
    Informational emails go straight to done so downstream steps
    that depend on this one unblock immediately.
    """
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)
    response = StepResponse(
        step_id=step.id,
        tenant_id=tenant.id,
        source="outbound_email_sent",
        outbound_message_id=body.outbound_message_id,
    )
    db.add(response)

    new_state: str
    if getattr(step, "awaits_response", False):
        new_state = "awaiting_response"
    else:
        new_state = "done"

    if step.state in {"ready", "blocked", "in_progress"}:
        step.state = new_state
        step.started_at = step.started_at or datetime.now(timezone.utc)
        if new_state == "done":
            step.completed_at = datetime.now(timezone.utc)
            # Unblock downstream steps that depend on this one.
            engine = ActionPlanEngine()
            await engine._propagate_completion(db, completed_step=step)  # noqa: SLF001

    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.state_changed",
        plan_id=plan_id, step_id=step_id,
        extra={"new_state": new_state},
    )
    return await _build_plan_out(db, plan)


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/schedule-meeting",
    response_model=ScheduleMeetingForStepResult,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def schedule_meeting_for_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: ScheduleMeetingForStepRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Schedule a calendar event for a meeting/phone_call step.

    Mirrors :func:`backend.app.api.action_items.schedule_meeting_for_action_item`
    but sourced from an ActionStep instead of the legacy ActionItem.
    Picks the best calendar provider for the user (Google → Microsoft →
    Zoom → Cal.com → stub), creates the event, stamps
    ``step.calendar_event_id`` on success, and returns the join URL or
    the stub's ICS payload.

    Core logic lives in :mod:`backend.app.services.action_plan.dispatch`
    so the governed auto-executor schedules through the exact same path.
    """
    from backend.app.services.action_plan.dispatch import dispatch_step_meeting

    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    organizer_email = (
        principal.user.email if principal.user and principal.user.email
        else None
    )
    user_id = principal.user.id if principal.user else None

    result = await dispatch_step_meeting(
        db,
        tenant=tenant,
        plan=plan,
        step=step,
        user_id=user_id,
        organizer_email=organizer_email,
        start=body.start,
        duration_minutes=body.duration_minutes,
        location=body.location,
        override_subject=body.override_subject,
        override_participants=body.override_participants,
        conference_provider=body.conference_provider,
    )

    await db.commit()
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.scheduled_meeting",
        plan_id=plan_id, step_id=step_id,
        extra={"provider": result.provider, "success": result.success},
    )

    return ScheduleMeetingForStepResult(
        success=result.success,
        provider=result.provider,
        event_id=result.event_id,
        join_url=result.join_url,
        html_link=result.html_link,
        ics_payload=result.ics_payload,
        note=result.note,
        error=result.error,
    )


# ──────────────────────────────────────────────────────────
# Per-step Send email
# ──────────────────────────────────────────────────────────


class SendStepEmailRequest(BaseModel):
    """Optional overrides for the per-step email send. When omitted, the
    artifact body and the participant-resolver output are used as-is."""
    to: Optional[str] = None
    cc: Optional[str] = None
    subject_override: Optional[str] = None
    body_override: Optional[str] = None
    provider: Optional[str] = None  # 'google' | 'microsoft'


class SendStepEmailResult(BaseModel):
    success: bool
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None
    email_send_id: Optional[uuid.UUID] = None
    error: Optional[str] = None


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/send-email",
    response_model=SendStepEmailResult,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def send_email_for_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: SendStepEmailRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Send the step's email artifact via the tenant's connected Gmail / Outlook.

    Uses the synthesizer-produced subject + body unless the caller
    overrides them. Resolves the To address from the first customer-side
    participant via :func:`resolve_participants` when not supplied.

    On success, transitions the step the same way ``POST .../sent``
    does: ``awaiting_response`` if ``step.awaits_response``, else
    ``done``. Records an ``email_sends`` row so the inbound matcher
    can tie a reply back to this step via RFC 822 headers.

    Core logic lives in :mod:`backend.app.services.action_plan.dispatch`
    so the governed auto-executor sends through the exact same path.
    """
    from backend.app.api.emails import _principal_email
    from backend.app.services.action_plan.dispatch import dispatch_step_email

    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    result = await dispatch_step_email(
        db,
        tenant=tenant,
        plan=plan,
        step=step,
        to=body.to,
        cc=body.cc,
        subject_override=body.subject_override,
        body_override=body.body_override,
        provider=body.provider,
        sender_user_id=principal.user.id if principal.user else None,
        principal_email_hint=_principal_email(principal),
    )
    await db.commit()

    if result.success:
        _emit_event(
            tenant=tenant, principal=principal,
            event="action_step.email_sent",
            plan_id=plan_id, step_id=step_id,
            extra={"provider": result.provider, "new_state": result.new_state},
        )
        return SendStepEmailResult(
            success=True,
            provider=result.provider,
            provider_message_id=result.provider_message_id,
            email_send_id=result.email_send_id,
        )

    if result.email_send_id is None:
        # No EmailSend row was created at all (bad request: no artifact,
        # no recipient, no integration) — preserve the 400 the endpoint
        # used to raise in those cases.
        raise HTTPException(status_code=400, detail=result.error)

    return SendStepEmailResult(
        success=False,
        provider=result.provider,
        email_send_id=result.email_send_id,
        error=result.error,
    )


# ──────────────────────────────────────────────────────────
# Per-step Commit to CRM (note + system_write)
# ──────────────────────────────────────────────────────────


class CommitStepRequest(BaseModel):
    """Optional override for the body that gets pushed to the CRM."""
    body_override: Optional[str] = None


class CommitStepResult(BaseModel):
    success: bool
    provider: Optional[str] = None
    external_id: Optional[str] = None
    error: Optional[str] = None


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/commit",
    response_model=CommitStepResult,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def commit_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: CommitStepRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Push a ``note`` or ``system_write`` step artifact into the
    tenant's connected CRM.

    For ``note`` channel: writes the artifact body as a CRM note on the
    interaction's customer (uses :func:`write_back_interaction`'s
    underlying note adapter selection).

    For ``system_write`` channel: executes the synthesizer-emitted
    payload against the named integration (e.g. HubSpot
    ``create_task``).

    On success, marks the step done and propagates completion.

    Core logic lives in :mod:`backend.app.services.action_plan.dispatch`
    so the governed auto-executor writes through the exact same path.
    """
    from backend.app.services.action_plan.dispatch import dispatch_step_commit

    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    if step.recommended_channel not in {"note", "system_write"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Step channel '{step.recommended_channel}' is not "
                "committable through this endpoint. Only 'note' and "
                "'system_write' use /commit. Email steps use "
                "/send-email; meeting/phone_call use /schedule-meeting."
            ),
        )

    result = await dispatch_step_commit(
        db, tenant=tenant, plan=plan, step=step, body_override=body.body_override,
    )
    if not result.success and result.error == "Step has no artifact to commit.":
        raise HTTPException(status_code=400, detail=result.error)

    await db.commit()
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.committed",
        plan_id=plan_id, step_id=step_id,
        extra={"provider": result.provider, "success": result.success},
    )
    return CommitStepResult(
        success=result.success,
        provider=result.provider,
        external_id=result.external_id,
        error=result.error,
    )


# ──────────────────────────────────────────────────────────
# Mark step sent / done manually (escape hatch for any channel)
# ──────────────────────────────────────────────────────────


class MarkStepSentRequest(BaseModel):
    """``source`` is a free-form tag ('phone_dialed', 'sent_from_gmail',
    'document_uploaded'). It's persisted on the StepResponse so the
    audit trail records how the rep claims the action got done."""
    source: str = Field(..., min_length=1, max_length=64)
    note: Optional[str] = Field(None, max_length=1000)


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/mark-sent",
    response_model=ActionPlanOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def mark_step_sent(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: MarkStepSentRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Manual escape hatch for any outbound-channel step.

    When the rep took the action outside the app (sent the email from
    their phone, made the call from their desk phone, faxed the
    document), this records that fact and transitions the step like a
    successful in-app send would. ``awaits_response=True`` steps land
    in ``awaiting_response``; the rest land in ``done`` and cascade.
    """
    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    response = StepResponse(
        step_id=step.id,
        tenant_id=tenant.id,
        source=f"manual:{body.source}",
        note_text=body.note,
    )
    db.add(response)

    new_state = "awaiting_response" if getattr(step, "awaits_response", False) else "done"
    if step.state in {"ready", "blocked", "in_progress"}:
        step.state = new_state
        step.started_at = step.started_at or datetime.now(timezone.utc)
        if new_state == "done":
            step.completed_at = datetime.now(timezone.utc)
            engine = ActionPlanEngine()
            await engine._propagate_completion(db, completed_step=step)  # noqa: SLF001

    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.marked_sent_manually",
        plan_id=plan_id, step_id=step_id,
        extra={"source": body.source, "new_state": new_state},
    )
    return await _build_plan_out(db, plan)


# ──────────────────────────────────────────────────────────
# Resolved attachments + participants (for in-step rendering)
# ──────────────────────────────────────────────────────────


class GenerateDocumentRequest(BaseModel):
    """Optional knobs for the per-step document generator."""
    attachment_title: Optional[str] = Field(
        None,
        description=(
            "Which of the synthesizer's suggested attachments to render. "
            "Defaults to step.title."
        ),
    )
    extra_instructions: Optional[str] = Field(
        None,
        max_length=2000,
        description="Optional free-form guidance to layer on top of the system prompt.",
    )


class GenerateDocumentOut(BaseModel):
    title: str
    body_markdown: str
    word_count: int
    model: str
    generated_at_unix: float


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/generate-document",
    response_model=GenerateDocumentOut,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def generate_document_for_plan_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: GenerateDocumentRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Generate a full Markdown document body for a document_send step.

    Calls Claude Sonnet against the step + source interaction context
    to produce a one-page document grounded in what the call actually
    said. The body is returned as Markdown; the SPA renders to HTML
    for inline preview and exposes browser print-to-PDF + download
    affordances. Cost: one Sonnet call (~$0.03 per page).
    """
    from backend.app.services.action_plan.document_generator import (
        generate_document_for_step,
    )

    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    # Permit on document_send AND email channels — sometimes a
    # synthesizer-emitted email step wants a longer attachment too.
    if step.recommended_channel not in {"document_send", "email"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Document generation is only meaningful for document_send "
                f"or email steps; this step's channel is "
                f"'{step.recommended_channel}'."
            ),
        )

    interaction: Optional[Interaction] = None
    if plan.interaction_id is not None:
        interaction = await db.get(Interaction, plan.interaction_id)
        if interaction is not None and interaction.tenant_id != tenant.id:
            interaction = None  # defensive — should not happen via auth

    try:
        result = await generate_document_for_step(
            db,
            step=step,
            interaction=interaction,
            attachment_title=body.attachment_title,
            extra_instructions=body.extra_instructions,
        )
    except Exception as exc:  # noqa: BLE001 — surface to caller
        logger.exception("generate_document_for_step failed")
        raise HTTPException(status_code=502, detail=f"Generation failed: {exc}")

    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.document_generated",
        plan_id=plan_id, step_id=step_id,
        extra={"word_count": result.word_count, "model": result.model},
    )
    return GenerateDocumentOut(
        title=result.title,
        body_markdown=result.body_markdown,
        word_count=result.word_count,
        model=result.model,
        generated_at_unix=result.generated_at_unix,
    )


# ──────────────────────────────────────────────────────────
# Draft-now: fire Call C for a single step (lazy-draft flow)
# ──────────────────────────────────────────────────────────


class DraftNowRequest(BaseModel):
    """Optional rep-provided values for unfilled slots. Use this when
    the step is ``draft_blocked`` (upstream was skipped or deleted) or
    when the rep wants to draft a ``pending_upstream`` step manually."""
    slot_overrides: Optional[Dict[str, Any]] = None


class DraftNowResult(BaseModel):
    success: bool
    draft_state: str
    artifact_version: int
    error: Optional[str] = None


@router.post(
    "/action-plans/{plan_id}/steps/{step_id}/draft-now",
    response_model=DraftNowResult,
    dependencies=[Depends(require_scope("action_items:write"))],
)
async def draft_step_now(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    body: DraftNowRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Render a Call C artifact for a single step on demand.

    Three call sites:

    * SPA clicks "Draft now" on a ``ready_to_draft`` step (all critical
      slots are filled but the synthesizer skipped drafting because the
      step was waiting on upstream completion when synthesis ran).
    * SPA clicks "Draft anyway" on a ``draft_blocked`` step (upstream
      that was the source of a critical slot got skipped). The
      ``slot_overrides`` map carries the rep's manual values for those
      missing slots.
    * SPA clicks "Re-draft" on a ``drafted`` step whose ``artifact_stale``
      flag is True (an upstream slot fill changed the data).

    Returns the step's new draft_state ("drafted" on success). The
    artifact body itself is reachable via the regular plan-detail
    endpoint, which is what the SPA already polls.
    """
    from backend.app.services.action_plan.synthesizer import (
        render_single_step_artifact,
    )

    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    interaction: Optional[Interaction] = None
    if plan.interaction_id is not None:
        interaction = await db.get(Interaction, plan.interaction_id)
        if interaction is not None and interaction.tenant_id != tenant.id:
            interaction = None

    try:
        await render_single_step_artifact(
            db,
            step=step,
            tenant=tenant,
            interaction=interaction,
            slot_overrides=body.slot_overrides,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("draft_step_now failed")
        raise HTTPException(status_code=502, detail=f"Draft failed: {exc}")

    await db.commit()
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.drafted_lazily",
        plan_id=plan_id, step_id=step_id,
        extra={"draft_state": step.draft_state},
    )
    return DraftNowResult(
        success=True,
        draft_state=step.draft_state,
        artifact_version=step.artifact_version,
    )


class ResolvedAttachmentOut(BaseModel):
    title: str
    reason: Optional[str] = None
    kb_doc_id: Optional[uuid.UUID] = None
    source_url: Optional[str] = None
    snippet: Optional[str] = None
    match_score: Optional[float] = None


class ResolvedParticipantOut(BaseModel):
    name: str
    role: Optional[str] = None
    side: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class StepResolvedOut(BaseModel):
    attachments: List[ResolvedAttachmentOut]
    participants: List[ResolvedParticipantOut]


@router.get(
    "/action-plans/{plan_id}/steps/{step_id}/resolved",
    response_model=StepResolvedOut,
    dependencies=[Depends(require_scope("action_items:read"))],
)
async def resolved_for_step(
    plan_id: uuid.UUID,
    step_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Resolve attachment titles to KB docs and participant names to
    contact emails, for in-step rendering on the SPA.

    Both resolutions are best-effort: an attachment that doesn't match
    any KB doc still appears in the list with ``kb_doc_id=None`` so the
    rep sees the synthesizer's suggestion; the SPA can render a "search
    KB" CTA. Same for participants without an email match.
    """
    from sqlalchemy import or_ as _or, func as _func
    from backend.app.models import KBDocument
    from backend.app.services.meeting_scheduler.participant_resolver import (
        resolve_participants,
    )

    plan = await _load_plan_or_404(db, tenant, plan_id)
    step = await _load_step_or_404(db, tenant, plan_id, step_id)

    # ─── Attachments ───
    # Source of suggested_attachments depends on channel:
    #  - email / document_send: artifact.payload.attachments (list of
    #    {title, reason}) — produced by the document_send schema
    #  - all other channels: step.prep_artifacts (list of strings)
    artifact_stmt = (
        select(StepArtifact)
        .where(StepArtifact.step_id == step.id, StepArtifact.tenant_id == tenant.id)
        .order_by(StepArtifact.generated_at.desc())
        .limit(1)
    )
    artifact = (await db.execute(artifact_stmt)).scalar_one_or_none()
    raw_attachments: List[Dict[str, Any]] = []
    if artifact and isinstance(artifact.payload, dict):
        payload_atts = artifact.payload.get("attachments")
        if isinstance(payload_atts, list):
            for a in payload_atts:
                if isinstance(a, dict) and a.get("title"):
                    raw_attachments.append(
                        {"title": str(a["title"]), "reason": a.get("reason")}
                    )
    # Fall back to step.prep_artifacts when the artifact didn't emit
    # attachments (most non-document_send channels).
    if not raw_attachments:
        for pa in step.prep_artifacts or []:
            if isinstance(pa, str) and pa.strip():
                raw_attachments.append({"title": pa.strip(), "reason": None})

    resolved_attachments: List[ResolvedAttachmentOut] = []
    for raw in raw_attachments:
        title_lower = raw["title"].lower()
        # ILIKE on title; pick the most-recent match. Cheap and
        # adequate for the demo; for real scale this should be a vector
        # similarity search against KB title embeddings.
        kb_stmt = (
            select(KBDocument)
            .where(
                KBDocument.tenant_id == tenant.id,
                _func.lower(KBDocument.title).ilike(f"%{title_lower}%"),
            )
            .order_by(KBDocument.created_at.desc())
            .limit(1)
        )
        kb_doc = (await db.execute(kb_stmt)).scalar_one_or_none()
        if kb_doc:
            resolved_attachments.append(
                ResolvedAttachmentOut(
                    title=raw["title"],
                    reason=raw.get("reason"),
                    kb_doc_id=kb_doc.id,
                    source_url=kb_doc.source_url,
                    snippet=(kb_doc.content or "")[:200] or None,
                    match_score=0.8,  # placeholder for future similarity score
                )
            )
        else:
            resolved_attachments.append(
                ResolvedAttachmentOut(
                    title=raw["title"],
                    reason=raw.get("reason"),
                )
            )

    # ─── Participants ───
    customer_id = None
    interaction_stmt = select(Interaction).where(
        Interaction.id == plan.interaction_id,
        Interaction.tenant_id == tenant.id,
    )
    interaction = (await db.execute(interaction_stmt)).scalar_one_or_none()
    if interaction:
        customer_id = interaction.customer_id
    resolved = await resolve_participants(
        db,
        tenant_id=tenant.id,
        customer_id=customer_id,
        raw_participants=step.participants or [],
    )
    resolved_participants = [
        ResolvedParticipantOut(
            name=p.name, role=p.role, side=p.side, email=p.email, phone=p.phone,
        )
        for p in resolved
    ]

    return StepResolvedOut(
        attachments=resolved_attachments,
        participants=resolved_participants,
    )
