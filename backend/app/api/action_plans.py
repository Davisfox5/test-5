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
from datetime import datetime
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


async def _build_plan_out(
    db: AsyncSession, plan: ActionPlan,
) -> ActionPlanOut:
    """Load steps + latest artifact + responses for the plan."""
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
    items = [await _build_plan_out(db, p) for p in plans]
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
    State moves from ready -> awaiting_response.
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
    if step.state in {"ready", "blocked"}:
        step.state = "awaiting_response"
        step.started_at = datetime.utcnow()
    await db.commit()
    await db.refresh(plan)
    _emit_event(
        tenant=tenant, principal=principal,
        event="action_step.state_changed",
        plan_id=plan_id, step_id=step_id,
        extra={"new_state": "awaiting_response"},
    )
    return await _build_plan_out(db, plan)
