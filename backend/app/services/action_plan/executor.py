"""Governed auto-executor — the ONLY code path that lets a policy (not a
human clicking a button) dispatch an action-plan step.

Safety contract (non-negotiable, see docs on ``settings.AUTO_EXECUTION_ENABLED``):

1. ``settings.AUTO_EXECUTION_ENABLED`` (default ``False``) gates the whole
   thing. :func:`run_due_executions` checks it FIRST and returns a no-op
   dict without touching the database when it's off — belt-and-braces
   with the beat task's own check in ``tasks.py``.
2. Per (tenant, action_class) policy defaults to ``manual`` (no row =
   manual). A step is only ever auto-dispatched when BOTH #1 is true AND
   the tenant explicitly set an auto mode for that step's action_class.
3. ``shadow`` mode logs "WOULD dispatch" + writes an audit
   ``StepResponse`` but performs no external side effect and leaves
   ``step.state`` untouched.
4. ``approve_then_auto`` moves the step to ``pending_approval`` and
   stops; only ``auto`` dispatches for real.

Reuse, not reimplementation: the actual send/commit/schedule logic is
:mod:`backend.app.services.action_plan.dispatch` — the exact code the
manual ``/send-email``, ``/commit``, ``/schedule-meeting`` endpoints call.
This module only decides WHETHER and WHEN to call it.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import (
    ActionPlan,
    ActionStep,
    AutoExecutionPolicy,
    StepArtifact,
    StepResponse,
    Tenant,
)
from backend.app.services.action_plan.dispatch import (
    dispatch_step_commit,
    dispatch_step_email,
    dispatch_step_meeting,
    latest_artifact_for_step,
)
from backend.app.services.pipeline_ledger import (
    STEP_AUTO_EXECUTION_DISPATCH,
    StepClaim,
    claim_step_async,
    compute_input_hash,
    complete_step_async,
    fail_step_async,
)

logger = logging.getLogger(__name__)

# Channel -> action_class bucket. Absent/unknown channels default to
# high_risk (fail safe: an unrecognized channel needs an explicit
# tenant opt-in, not a silent low-risk default).
LOW_RISK_CHANNELS = frozenset({"note", "research"})
HIGH_RISK_CHANNELS = frozenset(
    {"email", "meeting", "phone_call", "document_send", "system_write"}
)

# Channels this executor actually knows how to dispatch. 'research' and
# 'document_send' produce artifacts a rep reviews/downloads manually —
# there's no unattended "send" for them yet, so 'auto' mode is a no-op
# for those channels regardless of policy (shadow logging still works).
_DISPATCHABLE_CHANNELS = frozenset(
    {"email", "note", "system_write", "meeting", "phone_call"}
)


def action_class_for_step(step: ActionStep) -> str:
    """Bucket a step into 'low_risk' or 'high_risk' from its channel."""
    channel = (step.recommended_channel or "").lower()
    if channel in LOW_RISK_CHANNELS:
        return "low_risk"
    return "high_risk"


@dataclass
class ExecutionCounters:
    scanned: int = 0
    dispatched: int = 0
    shadow_logged: int = 0
    pending_approval: int = 0
    skipped_unfilled_slots: int = 0
    skipped_no_policy: int = 0
    skipped_unsupported_channel: int = 0
    skipped_already_dispatched: int = 0
    skipped_held: int = 0
    skipped_rate_capped: int = 0
    errors: int = 0

    def as_dict(self) -> Dict[str, int]:
        return dict(self.__dict__)


def _audit_response(
    *,
    step: ActionStep,
    tenant_id: uuid.UUID,
    mode: str,
    action_class: str,
    channel: str,
    worker_id: str,
    note: str,
) -> StepResponse:
    """The audit trail every real or shadow dispatch writes: who (the
    executor / worker_id), what (mode + channel), when (received_at,
    server default), which policy (mode + action_class)."""
    return StepResponse(
        step_id=step.id,
        tenant_id=tenant_id,
        source="auto_executed",
        note_text=note,
        extracted_data={
            "mode": mode,
            "action_class": action_class,
            "channel": channel,
            "worker_id": worker_id,
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
        },
    )


async def _dispatch_for_channel(
    db: AsyncSession, *, channel: str, tenant: Tenant, plan: ActionPlan, step: ActionStep,
):
    if channel == "email":
        return await dispatch_step_email(db, tenant=tenant, plan=plan, step=step)
    if channel in {"note", "system_write"}:
        return await dispatch_step_commit(db, tenant=tenant, plan=plan, step=step)
    if channel in {"meeting", "phone_call"}:
        return await dispatch_step_meeting(db, tenant=tenant, plan=plan, step=step)
    return None


async def run_due_executions(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: int = 50,
    worker_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Scan ``ready`` steps on ``active`` plans for ``tenant_id`` and act
    per each step's (tenant, action_class) policy. Returns a counters
    dict — never raises for a single step's dispatch failure (that step
    is logged + ledger-marked failed so a later tick retries it).

    Defense in depth: re-checks the global flag itself even though the
    beat task already gates on it, so calling this function directly
    (a test, a one-off script) can never bypass the kill switch.
    """
    settings = get_settings()
    if not settings.AUTO_EXECUTION_ENABLED:
        return {"enabled": False, "reason": "AUTO_EXECUTION_ENABLED is False"}

    counters = ExecutionCounters()
    worker_id = worker_id or f"auto-executor:{os.getpid()}"
    rate_cap = settings.AUTO_EXECUTION_MAX_DISPATCHES_PER_TENANT

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return {"enabled": True, **counters.as_dict()}

    policy_stmt = select(AutoExecutionPolicy).where(
        AutoExecutionPolicy.tenant_id == tenant_id
    )
    policies = {
        p.action_class: p.mode for p in (await db.execute(policy_stmt)).scalars()
    }

    steps_stmt = (
        select(ActionStep)
        .join(ActionPlan, ActionStep.plan_id == ActionPlan.id)
        .where(
            ActionStep.tenant_id == tenant_id,
            ActionStep.state == "ready",
            ActionPlan.status == "active",
        )
        .order_by(ActionStep.created_at)
        .limit(limit)
    )
    steps = list((await db.execute(steps_stmt)).scalars())

    processed = 0
    plan_cache: Dict[uuid.UUID, Optional[ActionPlan]] = {}

    for step in steps:
        counters.scanned += 1

        artifact = await latest_artifact_for_step(
            db, tenant_id=tenant_id, step_id=step.id
        )
        if artifact is None or not isinstance(artifact.payload, dict):
            counters.skipped_unfilled_slots += 1
            continue
        if artifact.payload.get("unfilled_slots"):
            counters.skipped_unfilled_slots += 1
            continue

        action_class = action_class_for_step(step)
        mode = policies.get(action_class, "manual")
        if mode == "manual":
            counters.skipped_no_policy += 1
            continue

        if processed >= rate_cap:
            counters.skipped_rate_capped += 1
            logger.warning(
                "auto-executor: tenant %s hit its per-tick rate cap (%d); "
                "stopping this tick, remaining ready steps retry next tick",
                tenant_id, rate_cap,
            )
            break

        plan = plan_cache.get(step.plan_id)
        if step.plan_id not in plan_cache:
            plan = await db.get(ActionPlan, step.plan_id)
            plan_cache[step.plan_id] = plan
        if plan is None:
            continue

        channel = (step.recommended_channel or "").lower()

        if mode == "shadow":
            logger.info(
                "auto-executor SHADOW: would dispatch step %s via channel=%s "
                "(tenant=%s, action_class=%s)",
                step.id, channel, tenant_id, action_class,
            )
            db.add(_audit_response(
                step=step, tenant_id=tenant_id, mode="shadow",
                action_class=action_class, channel=channel, worker_id=worker_id,
                note=f"[shadow] would dispatch via '{channel}'; no side effect performed.",
            ))
            await db.commit()
            counters.shadow_logged += 1
            processed += 1
            continue

        if mode == "approve_then_auto":
            if step.state != "ready":
                continue
            step.state = "pending_approval"
            db.add(_audit_response(
                step=step, tenant_id=tenant_id, mode="approve_then_auto",
                action_class=action_class, channel=channel, worker_id=worker_id,
                note=(
                    f"Moved to pending_approval for a '{channel}' dispatch; "
                    "awaiting human approval before auto-dispatch."
                ),
            ))
            await db.commit()
            counters.pending_approval += 1
            processed += 1
            continue

        if mode != "auto":  # pragma: no cover — CHECK constraint guards this
            counters.skipped_no_policy += 1
            continue

        if channel not in _DISPATCHABLE_CHANNELS:
            counters.skipped_unsupported_channel += 1
            continue

        # Idempotency: claim a durable ledger marker before doing anything
        # external, so a retry/redeploy between the claim and the state
        # commit resumes instead of double-sending. The ledger's natural
        # key is (interaction_id, step_key, input_hash); manually-created
        # plans (no interaction) have no durable key to claim against —
        # skip 'auto' for those rather than weaken the guarantee.
        run_id: Optional[uuid.UUID] = None
        if plan.interaction_id is not None:
            input_hash = compute_input_hash(step.id, artifact.version, mode)
            claim = await claim_step_async(
                db,
                tenant_id=tenant_id,
                interaction_id=plan.interaction_id,
                step_key=STEP_AUTO_EXECUTION_DISPATCH,
                input_hash=input_hash,
                worker_id=worker_id,
            )
            if claim.outcome == StepClaim.HELD:
                counters.skipped_held += 1
                continue
            if claim.outcome == StepClaim.REUSED:
                counters.skipped_already_dispatched += 1
                continue
            run_id = claim.run_id
        else:
            logger.warning(
                "auto-executor: step %s's plan has no interaction_id — no "
                "durable ledger key available; dispatching without the "
                "exactly-once guard (relies on the in-process ready-state "
                "check only)", step.id,
            )

        try:
            result = await _dispatch_for_channel(
                db, channel=channel, tenant=tenant, plan=plan, step=step,
            )
        except Exception as exc:  # noqa: BLE001 — never let one step kill the tick
            logger.exception("auto-executor: dispatch raised for step %s", step.id)
            if run_id is not None:
                await fail_step_async(db, run_id, error=str(exc)[:500])
            await db.commit()
            counters.errors += 1
            processed += 1
            continue

        if result is None or not result.success:
            error = getattr(result, "error", None) or "dispatch failed"
            if run_id is not None:
                await fail_step_async(db, run_id, error=str(error)[:500])
            await db.commit()
            counters.errors += 1
            logger.warning(
                "auto-executor: dispatch failed for step %s (channel=%s): %s",
                step.id, channel, error,
            )
            processed += 1
            continue

        db.add(_audit_response(
            step=step, tenant_id=tenant_id, mode="auto",
            action_class=action_class, channel=channel, worker_id=worker_id,
            note=f"Auto-dispatched via '{channel}'; step -> {result.new_state}.",
        ))
        if run_id is not None:
            await complete_step_async(
                db, run_id, output_digest=f"step.state={result.new_state}",
                commit=False,
            )
        await db.commit()
        counters.dispatched += 1
        processed += 1

    return {"enabled": True, **counters.as_dict()}


__all__ = [
    "LOW_RISK_CHANNELS",
    "HIGH_RISK_CHANNELS",
    "action_class_for_step",
    "run_due_executions",
]
