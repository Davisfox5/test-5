"""Action Plan execution engine - state machine + regen scheduler.

Concerns:

* **State transitions**: blocked / ready / in_progress / awaiting_response
  / done / skipped / deleted. Each transition is a single method here so
  callers (API handlers, the email matcher, the synthesizer) all go
  through the same gate.
* **Slot fill propagation**: when a step completes with output_data,
  flow those values into downstream input_slots, mark downstream
  artifacts stale, and schedule debounced regeneration.
* **Debounced regen**: 30s after the most recent slot fill, regenerate
  any stale downstream artifacts via Call C. The synthesizer's
  artifact-rendering machinery is reused so the prompts stay one place.
* **Cascade on skip/delete/edit**: per the locked decisions, skipped
  steps unblock downstream (treated like done with empty output); deleted
  steps strip themselves from downstream depends_on lists; output_data
  edits cascade as if they were a completion.

Notifications: every meaningful transition emits an event the SSE
notification layer ferries to the SPA. We use the existing
``notification_service.dispatch_action_plan_event`` shape (added in the
API module alongside this engine).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    ActionPlan,
    ActionStep,
    StepArtifact,
    StepResponse,
    Tenant,
)

logger = logging.getLogger(__name__)


# Locked cost decision: 30s debounce on downstream regen so a burst of
# slot fills coalesces into one Call C per affected step instead of N.
REGEN_DEBOUNCE_SECONDS = 30

# State sets used in transition guards.
TERMINAL_STATES = frozenset({"done", "skipped", "deleted"})
UNBLOCKING_STATES = frozenset({"done", "skipped"})  # skipped treated as done for unblocking


# ──────────────────────────────────────────────────────────
# Public state transitions
# ──────────────────────────────────────────────────────────


class ActionPlanEngine:
    """Coordinates state changes and downstream effects on a plan."""

    def __init__(self) -> None:
        # Synthesizer is heavy; lazy-imported so the engine can be used
        # in unit tests without the LLM client.
        self._synthesizer = None

    def _get_synthesizer(self):
        if self._synthesizer is None:
            from backend.app.services.action_plan.synthesizer import (
                ActionPlanSynthesizer,
            )
            self._synthesizer = ActionPlanSynthesizer()
        return self._synthesizer

    # ── Lifecycle: complete / skip / delete / reopen ──

    async def complete_step(
        self,
        db: AsyncSession,
        *,
        step: ActionStep,
        output_data: Optional[Dict[str, Any]] = None,
        source: str = "auto_mark_done",
        source_response_id: Optional[uuid.UUID] = None,
    ) -> List[uuid.UUID]:
        """Mark a step done; propagate slots; unblock + schedule regen.

        Returns the list of downstream step IDs whose state was updated.
        """
        if step.state in TERMINAL_STATES:
            return []
        step.state = "done"
        step.completed_at = datetime.now(timezone.utc)
        if output_data:
            merged = dict(step.output_data or {})
            merged.update(output_data)
            step.output_data = merged

        affected = await self._propagate_completion(
            db, completed_step=step,
        )
        await _check_plan_completion(db, plan_id=step.plan_id)
        return affected

    async def skip_step(
        self,
        db: AsyncSession,
        *,
        step: ActionStep,
        reason: Optional[str] = None,
    ) -> List[uuid.UUID]:
        """Mark a step skipped; downstream unblocks as if it was done."""
        if step.state in TERMINAL_STATES:
            return []
        step.state = "skipped"
        step.skipped_at = datetime.now(timezone.utc)
        if reason:
            step.skip_reason = reason[:2000]
        affected = await self._propagate_completion(
            db, completed_step=step, was_skipped=True,
        )
        await _check_plan_completion(db, plan_id=step.plan_id)
        return affected

    async def restore_step(
        self,
        db: AsyncSession,
        *,
        step: ActionStep,
    ) -> List[uuid.UUID]:
        """Undo a skip. Returns the step to ready/blocked based on deps.

        Downstream steps that were already propagated through (now ready
        or done assuming this one was skipped) get re-evaluated so they
        block again when appropriate. Only meaningful when the step is
        currently ``skipped``; a noop otherwise.
        """
        if step.state != "skipped":
            return []
        step.state = "ready"
        step.skipped_at = None
        step.skip_reason = None
        plan_steps = await _load_plan_steps(db, step.plan_id)
        # Re-evaluate THIS step's readiness in case its deps aren't done.
        await _evaluate_readiness(step, plan_steps)
        # Re-evaluate downstream steps too — they may have unlocked when
        # this step was skipped; restoring it might re-block them.
        affected: List[uuid.UUID] = []
        for s in plan_steps:
            if s.id == step.id:
                continue
            if str(step.id) in (s.depends_on or []):
                prev_state = s.state
                await _evaluate_readiness(s, plan_steps)
                if s.state != prev_state:
                    affected.append(s.id)
        return affected

    async def delete_step(
        self,
        db: AsyncSession,
        *,
        step: ActionStep,
    ) -> List[uuid.UUID]:
        """Soft-delete a step; strip it from downstream depends_on + slots.

        Per the locked policy: downstream artifacts become stale and
        regenerate. If a downstream had this step listed as a required
        slot filler, the slot stays unfilled and the regenerated artifact
        renders a placeholder.
        """
        if step.state == "deleted":
            return []
        step.state = "deleted"
        step.deleted_at = datetime.now(timezone.utc)

        plan_steps = await _load_plan_steps(db, step.plan_id)
        affected: List[uuid.UUID] = []
        for s in plan_steps:
            if s.id == step.id:
                continue
            changed = False
            new_deps = [d for d in (s.depends_on or []) if d != str(step.id)]
            if new_deps != (s.depends_on or []):
                s.depends_on = new_deps
                changed = True
            # Strip slot references to the deleted step.
            new_slots = []
            for slot in s.input_slots or []:
                if not isinstance(slot, dict):
                    continue
                if slot.get("filled_by_step_id") == str(step.id):
                    new_slot = dict(slot)
                    new_slot["filled_by_step_id"] = None
                    new_slot["filled_value"] = None
                    new_slot["filled_at"] = None
                    new_slots.append(new_slot)
                    changed = True
                else:
                    new_slots.append(slot)
            if changed:
                s.input_slots = new_slots
                _mark_stale_and_schedule(s)
                affected.append(s.id)
            # Re-evaluate readiness.
            await _evaluate_readiness(s, plan_steps)
        return affected

    async def reopen_step(
        self,
        db: AsyncSession,
        *,
        step: ActionStep,
    ) -> None:
        """Return a done/skipped step to the active workflow."""
        if step.state not in TERMINAL_STATES:
            return
        step.state = "ready"
        step.completed_at = None
        step.skipped_at = None
        step.skip_reason = None
        # Downstream that already saw this step as 'done' may need to
        # re-evaluate now that it's not - but we don't roll back their
        # filled slot values. Cascade is one-way.

    async def apply_response(
        self,
        db: AsyncSession,
        *,
        step: ActionStep,
        response: StepResponse,
    ) -> List[uuid.UUID]:
        """Apply an inbound email / note / extraction to a step.

        Auto-applies extracted_data to step.output_data, then completes
        the step if all required output_schema slots are filled. Per
        the locked decision: auto-apply with agent override; partial
        extractions leave the step in awaiting_response.
        """
        extracted = response.extracted_data or {}
        if extracted:
            merged = dict(step.output_data or {})
            merged.update(extracted)
            step.output_data = merged

        # Are all REQUIRED output_schema slots filled?
        required_keys = [
            s.get("slot_key")
            for s in (step.output_schema or [])
            if isinstance(s, dict) and s.get("required", True)
        ]
        all_filled = all(
            k in (step.output_data or {}) and step.output_data[k] is not None
            for k in required_keys
        )

        if all_filled or not required_keys:
            return await self.complete_step(
                db,
                step=step,
                output_data=None,  # already merged above
                source=response.source,
                source_response_id=response.id,
            )

        step.state = "awaiting_response"
        # Even partial fills propagate so downstream drafts can show
        # what they have.
        affected = await self._propagate_partial_fill(db, step=step)
        return affected

    # ── Propagation ──

    async def _propagate_completion(
        self,
        db: AsyncSession,
        *,
        completed_step: ActionStep,
        was_skipped: bool = False,
    ) -> List[uuid.UUID]:
        plan_steps = await _load_plan_steps(db, completed_step.plan_id)
        affected: List[uuid.UUID] = []
        for downstream in plan_steps:
            if downstream.id == completed_step.id:
                continue
            if str(completed_step.id) not in (downstream.depends_on or []):
                continue
            # Fill any input_slots that this completion satisfies.
            new_slots = []
            slot_changed = False
            for slot in downstream.input_slots or []:
                if not isinstance(slot, dict):
                    new_slots.append(slot)
                    continue
                if slot.get("filled_by_step_id") == str(completed_step.id):
                    slot_key = slot.get("slot_key")
                    new_slot = dict(slot)
                    val = (completed_step.output_data or {}).get(slot_key)
                    if val is not None and slot.get("filled_value") != val:
                        new_slot["filled_value"] = val
                        new_slot["filled_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                        slot_changed = True
                    new_slots.append(new_slot)
                else:
                    new_slots.append(slot)
            if slot_changed or was_skipped:
                downstream.input_slots = new_slots
                _mark_stale_and_schedule(downstream)
            # Re-evaluate readiness for downstream.
            await _evaluate_readiness(downstream, plan_steps)
            # Re-classify draft_state. Three cases:
            #  1. completed_step was skipped AND it provided one of
            #     downstream's CRITICAL slots that's still unfilled
            #     -> draft_blocked (rep needs to draft anyway or skip)
            #  2. downstream's CRITICAL slots are now all filled AND
            #     downstream hasn't been drafted yet
            #     -> ready_to_draft (SPA shows a "Draft now" button;
            #        rep clicks to fire Call C for this step)
            #  3. downstream still has unfilled critical slots
            #     -> pending_upstream (no change from synthesis)
            # Steps already in ``drafted`` keep that state regardless;
            # ``artifact_stale=True`` is the signal that an existing
            # draft should be regenerated, separate from draft_state.
            if downstream.draft_state != "drafted":
                _reclassify_draft_state(
                    downstream, was_skipped_upstream=was_skipped,
                )
            affected.append(downstream.id)
        return affected

    async def _propagate_partial_fill(
        self,
        db: AsyncSession,
        *,
        step: ActionStep,
    ) -> List[uuid.UUID]:
        """For a step that has new output_data but isn't done yet, push
        filled values into downstream slots so downstream drafts can
        show partial info. Downstream readiness does NOT advance until
        this step is fully done."""
        plan_steps = await _load_plan_steps(db, step.plan_id)
        affected: List[uuid.UUID] = []
        for downstream in plan_steps:
            if downstream.id == step.id:
                continue
            if str(step.id) not in (downstream.depends_on or []):
                continue
            new_slots = []
            slot_changed = False
            for slot in downstream.input_slots or []:
                if not isinstance(slot, dict):
                    new_slots.append(slot)
                    continue
                if slot.get("filled_by_step_id") == str(step.id):
                    slot_key = slot.get("slot_key")
                    new_slot = dict(slot)
                    val = (step.output_data or {}).get(slot_key)
                    if val is not None and slot.get("filled_value") != val:
                        new_slot["filled_value"] = val
                        new_slot["filled_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                        slot_changed = True
                    new_slots.append(new_slot)
                else:
                    new_slots.append(slot)
            if slot_changed:
                downstream.input_slots = new_slots
                _mark_stale_and_schedule(downstream)
                affected.append(downstream.id)
        return affected


# ──────────────────────────────────────────────────────────
# Module-level helpers (called from engine + other surfaces)
# ──────────────────────────────────────────────────────────


def _reclassify_draft_state(
    step: ActionStep,
    *,
    was_skipped_upstream: bool = False,
) -> None:
    """Re-evaluate a non-drafted step's ``draft_state`` after one of
    its upstream slots changes.

    Three terminal classifications:

    * ``draft_blocked`` — an upstream that the synthesizer pointed at
      for a CRITICAL slot was skipped, leaving that critical slot
      unfillable without manual intervention. The SPA shows a "Draft
      anyway" affordance with an inline form for the rep to supply
      the missing value.
    * ``ready_to_draft`` — all critical slots are now filled. The SPA
      shows a "Draft now" button that fires Call C via the
      /draft-now endpoint. (We don't auto-fire from the engine in v1
      to keep the engine sync-only; v2 can enqueue a Celery render.)
    * ``pending_upstream`` — at least one critical slot is still
      unfilled and depends on another upstream not yet complete.

    Already-drafted steps are NOT downgraded by this helper; their
    ``artifact_stale`` flag is the separate signal for "regenerate me."
    """
    if step.draft_state == "drafted":
        return

    critical_slots = [
        s for s in (step.input_slots or [])
        if isinstance(s, dict) and s.get("critical")
    ]
    critical_unfilled = [
        s for s in critical_slots if s.get("filled_value") is None
    ]

    if was_skipped_upstream and critical_unfilled:
        step.draft_state = "draft_blocked"
        return

    if not critical_unfilled:
        step.draft_state = "ready_to_draft"
        return

    step.draft_state = "pending_upstream"


def _mark_stale_and_schedule(step: ActionStep) -> None:
    """Mark a step's artifact stale and bump the regen-debounce window.

    The scheduler (a periodic Celery beat task or a tick on incoming
    SSE) reads ``regen_debounce_until``; the regen fires when now >=
    that value. Per the locked 30s debounce, a fresh slot fill resets
    the timer so a burst of fills coalesces into one regen.
    """
    step.artifact_stale = True
    step.regen_debounce_until = datetime.now(timezone.utc) + timedelta(
        seconds=REGEN_DEBOUNCE_SECONDS
    )


async def _load_plan_steps(
    db: AsyncSession, plan_id: uuid.UUID,
) -> List[ActionStep]:
    rows = await db.execute(
        select(ActionStep).where(ActionStep.plan_id == plan_id)
    )
    return list(rows.scalars())


async def _evaluate_readiness(
    step: ActionStep,
    plan_steps: Sequence[ActionStep],
) -> None:
    """Recompute state for a single step based on its dependencies.

    Only flips between ``blocked`` and ``ready`` here. Other states
    (in_progress, awaiting_response, done, skipped, deleted) are owned
    by explicit transitions and never overwritten by readiness checks.
    """
    if step.state not in {"blocked", "ready"}:
        return
    if not step.depends_on:
        step.state = "ready"
        return
    by_id = {str(s.id): s for s in plan_steps}
    all_unblocked = all(
        by_id.get(dep) is not None and by_id[dep].state in UNBLOCKING_STATES
        for dep in step.depends_on
    )
    step.state = "ready" if all_unblocked else "blocked"


async def _check_plan_completion(
    db: AsyncSession, *, plan_id: uuid.UUID,
) -> None:
    """If every step is terminal, mark the plan completed."""
    plan = await db.get(ActionPlan, plan_id)
    if plan is None or plan.status in {"completed", "abandoned"}:
        return
    steps = await _load_plan_steps(db, plan_id)
    if not steps:
        return
    if all(s.state in TERMINAL_STATES for s in steps):
        plan.status = "completed"
        plan.completed_at = datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────
# Regen scheduler — invoked from a Celery beat tick.
# ──────────────────────────────────────────────────────────


async def run_due_regenerations(
    db: AsyncSession,
    *,
    tenant_id: Optional[uuid.UUID] = None,
    limit: int = 50,
) -> int:
    """Pick up steps whose debounce timer has elapsed; regenerate them.

    Returns the number of artifacts regenerated. Tenant-scoped when
    ``tenant_id`` is provided (per-tenant beat tasks); global otherwise.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(ActionStep)
        .where(
            ActionStep.artifact_stale == True,  # noqa: E712
            ActionStep.regen_debounce_until <= now,
            ActionStep.state.in_(
                ("blocked", "ready", "in_progress", "awaiting_response")
            ),
        )
        .order_by(ActionStep.regen_debounce_until)
        .limit(limit)
    )
    if tenant_id is not None:
        stmt = stmt.where(ActionStep.tenant_id == tenant_id)

    rows = await db.execute(stmt)
    steps = list(rows.scalars())
    if not steps:
        return 0

    # Group by plan so the synthesizer can share the customer brief +
    # retrieval result across multiple step regens of the same plan
    # without re-fetching. The synthesizer's per-step Call C method
    # accepts the cached context directly; the dispatcher below pulls
    # context once per plan.
    by_plan: Dict[uuid.UUID, List[ActionStep]] = {}
    for s in steps:
        by_plan.setdefault(s.plan_id, []).append(s)

    regenerated = 0
    for plan_id, plan_steps in by_plan.items():
        plan = await db.get(ActionPlan, plan_id)
        if plan is None:
            continue
        tenant = await db.get(Tenant, plan.tenant_id)
        if tenant is None:
            continue
        try:
            regenerated += await _regenerate_steps_for_plan(
                db, plan=plan, tenant=tenant, steps=plan_steps,
            )
        except Exception:  # noqa: BLE001 - one plan's failure doesn't kill the batch
            logger.exception(
                "Regeneration failed for plan %s; will retry on next tick",
                plan_id,
            )
    return regenerated


async def _regenerate_steps_for_plan(
    db: AsyncSession,
    *,
    plan: ActionPlan,
    tenant: Tenant,
    steps: List[ActionStep],
) -> int:
    """Render new artifacts for ``steps`` of ``plan`` via Call C.

    Reuses the synthesizer's artifact-rendering machinery; the
    template, retrieval, and external context are reconstructed from
    the plan's stored snapshots so we don't pay for fresh retrieval on
    every regen.
    """
    from backend.app.services.action_plan.domains import get as get_domain
    from backend.app.services.action_plan.external_context import (
        ExternalContextResult,
        CrmCustomerSnapshot,
    )
    from backend.app.services.action_plan.prompts import (
        CALL_C_PAYLOAD_SCHEMAS,
        CALL_C_SYSTEM_PROMPT,
    )
    from backend.app.services.action_plan.synthesizer import (
        ActionPlanSynthesizer,
        _artifact_kind_for_channel,
        _format_filled_slots,
        _format_output_schema,
        _format_participants,
        _summary_block_for_artifact,
    )

    template = get_domain(plan.domain)
    synth = ActionPlanSynthesizer()

    # Rehydrate the external context snapshot saved at synthesis time.
    snapshot = plan.external_context_snapshot or {}
    rehydrated = ExternalContextResult(
        connected_providers=list(snapshot.get("connected_providers") or []),
        snapshots=[
            CrmCustomerSnapshot(
                provider=s.get("provider"),
                deals=s.get("deals") or [],
                last_synced_at=_parse_iso(s.get("last_synced_at")),
                is_stale=bool(s.get("is_stale")),
                error_reason=s.get("error_reason"),
            )
            for s in (snapshot.get("snapshots") or [])
            if isinstance(s, dict) and s.get("provider")
        ],
    )

    regenerated = 0
    for step in steps:
        channel = (step.recommended_channel or "note").lower()
        schema = CALL_C_PAYLOAD_SCHEMAS.get(channel) or CALL_C_PAYLOAD_SCHEMAS["note"]
        is_endpoint = step.role_in_plan == "customer_endpoint"
        tier = "sonnet" if is_endpoint else "haiku"

        system_prompt = CALL_C_SYSTEM_PROMPT.format(
            domain_role=template.role,
            tone=template.tone,
            tone_description=template.tone_description,
            tenant_name=tenant.name,
            summary_block=_summary_block_for_artifact({"goal": plan.goal}),
            customer_brief_block=rehydrated.to_brief_block(),
            step_title=step.title,
            step_intent=step.intent or step.description or "",
            step_channel=channel,
            step_participants=_format_participants(step.participants),
            filled_slots_block=_format_filled_slots(step.input_slots),
            output_schema_block=_format_output_schema(step.output_schema),
            kb_template_block="(no template in KB)",
            payload_schema_block=schema,
        )
        user_content = (
            f"Re-render the {channel} artifact now (slot data may have "
            "changed since the previous version). Return ONLY the JSON "
            "per the schema in the system prompt."
        )

        try:
            payload = await synth._call_with_retry(  # noqa: SLF001
                system_prompt=system_prompt,
                user_content=user_content,
                primary_tier=tier,
                max_tokens=2500 if not is_endpoint else 4000,
                label="action_plan.regen_call_c",
            )
        except Exception:  # noqa: BLE001 - skip this step; debounce stays so a later tick retries
            logger.exception("Regen Call C failed for step %s", step.id)
            continue

        if not isinstance(payload, dict):
            continue

        new_version = (step.artifact_version or 0) + 1
        artifact = StepArtifact(
            step_id=step.id,
            tenant_id=tenant.id,
            version=new_version,
            kind=_artifact_kind_for_channel(channel),
            payload=payload,
            model_tier=tier,
        )
        db.add(artifact)
        step.artifact_version = new_version
        step.artifact_stale = False
        step.regen_debounce_until = None
        regenerated += 1
    return regenerated


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


__all__ = [
    "ActionPlanEngine",
    "REGEN_DEBOUNCE_SECONDS",
    "TERMINAL_STATES",
    "run_due_regenerations",
]
