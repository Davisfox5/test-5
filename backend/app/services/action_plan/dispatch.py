"""Core dispatch logic for action-step side effects (email / CRM / meeting).

This is the SINGLE place that actually sends an email, writes a CRM
note/task, or books a calendar event for an ``ActionStep``. It exists so
two very different callers — the manual per-step endpoints in
``backend.app.api.action_plans`` (a rep clicking "Send") and the governed
auto-executor in ``backend.app.services.action_plan.executor`` (nothing
clicked; a policy decided) — run the EXACT same provider/CRM code path
instead of two copies that can drift.

Each ``dispatch_step_*`` function:

* does the channel-specific work (resolve integration, call the
  provider/adapter, record the outbound audit row);
* on success, transitions ``step.state`` exactly like the pre-existing
  manual endpoints did (``awaiting_response`` if ``step.awaits_response``,
  else ``done`` + ``ActionPlanEngine._propagate_completion``);
* does **not** commit — the caller (endpoint or executor) owns the
  transaction and commits once, alongside whatever audit/ledger rows it
  is also writing.

None of these functions know or care who is calling them (human via API,
or the executor); they take plain domain objects instead of FastAPI
request/principal types.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    ActionPlan,
    ActionStep,
    EmailSend,
    Interaction,
    StepArtifact,
    StepResponse,
    Tenant,
)
from backend.app.services.action_plan.engine import ActionPlanEngine

logger = logging.getLogger(__name__)


@dataclass
class EmailDispatchResult:
    success: bool
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None
    email_send_id: Optional[uuid.UUID] = None
    new_state: Optional[str] = None
    error: Optional[str] = None


@dataclass
class CommitDispatchResult:
    success: bool
    provider: Optional[str] = None
    external_id: Optional[str] = None
    new_state: Optional[str] = None
    error: Optional[str] = None


@dataclass
class MeetingDispatchResult:
    success: bool
    provider: Optional[str] = None
    event_id: Optional[str] = None
    join_url: Optional[str] = None
    html_link: Optional[str] = None
    ics_payload: Optional[str] = None
    note: Optional[str] = None
    new_state: Optional[str] = None
    error: Optional[str] = None


async def latest_artifact_for_step(
    db: AsyncSession, *, tenant_id: uuid.UUID, step_id: uuid.UUID,
) -> Optional[StepArtifact]:
    stmt = (
        select(StepArtifact)
        .where(StepArtifact.step_id == step_id, StepArtifact.tenant_id == tenant_id)
        .order_by(StepArtifact.generated_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _transition_on_send(step: ActionStep) -> str:
    """Same post-send transition every channel uses: awaiting_response if
    the step expects a reply, else done (+ propagate completion by the
    caller — callers await ``_apply_terminal_transition`` for that)."""
    return "awaiting_response" if getattr(step, "awaits_response", False) else "done"


async def _apply_terminal_transition(db: AsyncSession, *, step: ActionStep) -> str:
    """Flip a ready/blocked/in_progress step to its post-dispatch state and
    propagate completion when that state is terminal (``done``)."""
    new_state = _transition_on_send(step)
    if step.state in {"ready", "blocked", "in_progress"}:
        step.state = new_state
        step.started_at = step.started_at or datetime.now(timezone.utc)
        if new_state == "done":
            step.completed_at = datetime.now(timezone.utc)
            engine = ActionPlanEngine()
            await engine._propagate_completion(db, completed_step=step)  # noqa: SLF001
    return new_state


async def dispatch_step_email(
    db: AsyncSession,
    *,
    tenant: Tenant,
    plan: ActionPlan,
    step: ActionStep,
    to: Optional[str] = None,
    cc: Optional[str] = None,
    subject_override: Optional[str] = None,
    body_override: Optional[str] = None,
    provider: Optional[str] = None,
    sender_user_id: Optional[uuid.UUID] = None,
    principal_email_hint: Optional[str] = None,
) -> EmailDispatchResult:
    """Send the step's email artifact via the tenant's connected Gmail /
    Outlook. Identical behavior to the ``/send-email`` endpoint, minus the
    HTTP request/response shaping."""
    from backend.app.api.emails import (
        _build_sender,
        _close_sender,
        _resolve_integration,
    )
    from backend.app.services.email.base import EmailAuthError, EmailSendError
    from backend.app.services.meeting_scheduler.participant_resolver import (
        resolve_participants,
    )

    artifact = await latest_artifact_for_step(db, tenant_id=tenant.id, step_id=step.id)
    if artifact is None or not isinstance(artifact.payload, dict):
        return EmailDispatchResult(
            success=False,
            error="Step has no artifact to send. Wait for synthesis to complete or use override fields.",
        )

    payload = artifact.payload
    subject = subject_override or payload.get("subject") or step.title or ""
    body_text = body_override or payload.get("body") or ""
    if not subject or not body_text:
        return EmailDispatchResult(
            success=False,
            error="Subject and body required (supply override or wait for artifact).",
        )

    to_address = to
    if not to_address:
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
        first_customer = next(
            (p for p in resolved if (p.side or "").lower() == "customer" and p.email),
            None,
        )
        if first_customer is None:
            return EmailDispatchResult(
                success=False,
                error=(
                    "No customer recipient resolved. Either pass `to` "
                    "explicitly or add the contact to the customer's "
                    "Contact list with an email."
                ),
            )
        to_address = first_customer.email

    integ = await _resolve_integration(db, tenant.id, provider)
    if integ is None:
        return EmailDispatchResult(
            success=False,
            error="No Gmail or Outlook integration connected. Connect one under Settings.",
        )

    record = EmailSend(
        tenant_id=tenant.id,
        interaction_id=plan.interaction_id,
        sender_user_id=sender_user_id,
        provider=integ.provider,
        to_address=to_address,
        cc_address=cc,
        subject=subject,
        body=body_text,
        attachments=[],
        status="pending",
    )
    db.add(record)
    await db.flush()

    sender = _build_sender(integ, principal_email_hint=principal_email_hint)
    try:
        result = await sender.send(
            to=[to_address],
            subject=subject,
            body=body_text,
            cc=[cc] if cc else None,
        )
        record.status = "sent"
        record.provider_message_id = result.provider_message_id or result.message_id
        record.sent_at = datetime.now(timezone.utc)
    except EmailAuthError as exc:
        record.status = "failed"
        record.error = f"auth: {exc}"[:500]
        return EmailDispatchResult(
            success=False, provider=integ.provider, email_send_id=record.id,
            error=f"auth: {exc}",
        )
    except EmailSendError as exc:
        record.status = "failed"
        record.error = str(exc)[:500]
        return EmailDispatchResult(
            success=False, provider=integ.provider, email_send_id=record.id,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - persist failure, don't 500
        logger.exception("dispatch_step_email: unexpected provider failure")
        record.status = "failed"
        record.error = f"{type(exc).__name__}: {exc}"[:500]
        return EmailDispatchResult(
            success=False, provider=integ.provider, email_send_id=record.id,
            error="Email provider send failed",
        )
    finally:
        await _close_sender(sender)

    new_state = await _apply_terminal_transition(db, step=step)

    response = StepResponse(
        step_id=step.id,
        tenant_id=tenant.id,
        source="outbound_email_sent",
        outbound_message_id=record.provider_message_id or "",
    )
    db.add(response)

    return EmailDispatchResult(
        success=True,
        provider=integ.provider,
        provider_message_id=record.provider_message_id,
        email_send_id=record.id,
        new_state=new_state,
    )


async def dispatch_step_commit(
    db: AsyncSession,
    *,
    tenant: Tenant,
    plan: ActionPlan,
    step: ActionStep,
    body_override: Optional[str] = None,
) -> CommitDispatchResult:
    """Push a ``note``/``system_write`` step artifact to the tenant's
    connected CRM. Identical behavior to the ``/commit`` endpoint."""
    if step.recommended_channel not in {"note", "system_write"}:
        return CommitDispatchResult(
            success=False,
            error=(
                f"Step channel '{step.recommended_channel}' is not "
                "committable through this path. Only 'note' and "
                "'system_write' are supported."
            ),
        )

    artifact = await latest_artifact_for_step(db, tenant_id=tenant.id, step_id=step.id)
    if artifact is None or not isinstance(artifact.payload, dict):
        return CommitDispatchResult(success=False, error="Step has no artifact to commit.")

    payload = artifact.payload
    provider: Optional[str] = None
    external_id: Optional[str] = None

    try:
        from backend.app.services.crm.writeback import (
            _load_writeback_adapter,
            _pick_provider_for_writeback,
        )
        from backend.app.models import Contact, Customer

        interaction_stmt = select(Interaction).where(
            Interaction.id == plan.interaction_id,
            Interaction.tenant_id == tenant.id,
        )
        interaction = (await db.execute(interaction_stmt)).scalar_one_or_none()
        if interaction is None:
            raise RuntimeError("Plan's source interaction not found.")

        provider = await _pick_provider_for_writeback(db, tenant, interaction)
        if provider is None:
            raise RuntimeError(
                "No CRM integration connected. Connect HubSpot, Salesforce, "
                "or Pipedrive under Settings."
            )
        adapter = await _load_writeback_adapter(db, tenant.id, provider)
        if adapter is None:
            raise RuntimeError(f"Failed to instantiate {provider} CRM adapter.")

        contact_external_id: Optional[str] = None
        customer_external_id: Optional[str] = None
        if interaction.contact_id is not None:
            contact = await db.get(Contact, interaction.contact_id)
            if contact and contact.crm_id:
                contact_external_id = contact.crm_id
            if contact and contact.customer_id is not None:
                customer = await db.get(Customer, contact.customer_id)
                if customer and customer.crm_id:
                    customer_external_id = customer.crm_id

        if step.recommended_channel == "note":
            note_body = body_override or payload.get("body") or ""
            if not note_body:
                raise RuntimeError("Note body is empty.")
            external_id = await adapter.create_note(
                content=note_body,
                contact_external_id=contact_external_id,
                customer_external_id=customer_external_id,
            )
        else:  # system_write
            operation = payload.get("operation") or step.integration_operation or ""
            op_payload = payload.get("payload") or {}
            if not isinstance(op_payload, dict):
                op_payload = {}
            if not operation:
                raise RuntimeError(
                    "system_write step missing 'operation' on artifact payload."
                )
            external_id = await adapter.execute_operation(
                operation=str(operation),
                payload=op_payload,
                contact_external_id=op_payload.get("contact_external_id") or contact_external_id,
                customer_external_id=op_payload.get("customer_external_id") or customer_external_id,
                deal_external_id=op_payload.get("deal_external_id"),
            )
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
        logger.exception("dispatch_step_commit failed")
        return CommitDispatchResult(success=False, provider=provider, error=str(exc)[:500])

    if step.state in {"ready", "blocked", "in_progress"}:
        step.state = "done"
        step.started_at = step.started_at or datetime.now(timezone.utc)
        step.completed_at = datetime.now(timezone.utc)
        engine = ActionPlanEngine()
        await engine._propagate_completion(db, completed_step=step)  # noqa: SLF001

    return CommitDispatchResult(
        success=True, provider=provider, external_id=external_id, new_state="done",
    )


async def dispatch_step_meeting(
    db: AsyncSession,
    *,
    tenant: Tenant,
    plan: ActionPlan,
    step: ActionStep,
    user_id: Optional[uuid.UUID] = None,
    organizer_email: Optional[str] = None,
    start: Optional[datetime] = None,
    duration_minutes: int = 30,
    location: Optional[str] = None,
    override_subject: Optional[str] = None,
    override_participants: Optional[List[Any]] = None,
    conference_provider: Optional[str] = None,
) -> MeetingDispatchResult:
    """Schedule a calendar event for a meeting/phone_call step. Identical
    behavior to the ``/schedule-meeting`` endpoint."""
    from backend.app.services.meeting_scheduler import (
        MeetingRequest,
        MeetingScheduler,
    )
    from backend.app.services.meeting_scheduler.participant_resolver import (
        resolve_participants,
    )

    interaction_stmt = select(Interaction).where(
        Interaction.id == plan.interaction_id,
        Interaction.tenant_id == tenant.id,
    )
    interaction = (await db.execute(interaction_stmt)).scalar_one_or_none()
    customer_id = interaction.customer_id if interaction else None

    raw_parts = override_participants
    if raw_parts is None:
        raw_parts = step.participants or []
    resolved = await resolve_participants(
        db, tenant_id=tenant.id, customer_id=customer_id, raw_participants=raw_parts,
    )

    subject = override_subject or step.title or "Meeting"
    description_parts = [step.description or ""]
    if step.channel_reasoning:
        description_parts.append(f"\n\nWhy meeting: {step.channel_reasoning}")
    if step.prep_artifacts:
        description_parts.append("\n\nPrep:")
        for artifact in step.prep_artifacts:
            if isinstance(artifact, str) and artifact.strip():
                description_parts.append(f"\n  - {artifact}")
    body_text = "".join(description_parts).strip()

    inferred_conference = conference_provider
    inferred_location = location
    if inferred_conference is None and step.recommended_channel == "phone_call":
        inferred_conference = "none"
        customer_phone = next(
            (
                getattr(p, "phone", None)
                for p in resolved
                if (p.side or "").lower() == "customer" and getattr(p, "phone", None)
            ),
            None,
        )
        if not customer_phone and interaction:
            customer_phone = getattr(interaction, "caller_phone", None)
        if customer_phone:
            inferred_location = inferred_location or f"Phone: {customer_phone}"
            body_text = f"Call: {customer_phone}\n\n{body_text}"

    request = MeetingRequest(
        subject=subject,
        body=body_text,
        organizer_email=organizer_email or "no-reply@linda.local",
        participants=resolved,
        start=start,
        duration_minutes=duration_minutes,
        conference_provider=inferred_conference,
        location=inferred_location,
    )

    tf = getattr(tenant, "features_enabled", None) or {}
    preferred = tf.get("calendar_provider") if isinstance(tf, dict) else None
    scheduler = MeetingScheduler(
        db, tenant_id=tenant.id, user_id=user_id, preferred_provider=preferred,
    )
    result_obj = await scheduler.create_meeting(request)

    if result_obj.success and result_obj.event_id:
        step.calendar_event_id = result_obj.event_id

    new_state: Optional[str] = None
    if result_obj.success:
        new_state = await _apply_terminal_transition(db, step=step)

    return MeetingDispatchResult(
        success=result_obj.success,
        provider=result_obj.provider,
        event_id=result_obj.event_id,
        join_url=result_obj.join_url,
        html_link=result_obj.html_link,
        ics_payload=result_obj.ics_payload,
        note=result_obj.note,
        error=result_obj.error,
        new_state=new_state,
    )


__all__ = [
    "EmailDispatchResult",
    "CommitDispatchResult",
    "MeetingDispatchResult",
    "latest_artifact_for_step",
    "dispatch_step_email",
    "dispatch_step_commit",
    "dispatch_step_meeting",
]
