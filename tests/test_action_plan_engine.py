"""Action Plan Engine — state transitions, slot propagation, cascades.

Tests cover:
* complete_step: propagates output_data into downstream filled slots,
  marks downstream stale + schedules regen, advances readiness.
* skip_step: treated like done for unblocking; no value flows.
* delete_step: strips from downstream depends_on + clears slot pointers.
* apply_response: full extraction completes the step; partial leaves
  it in awaiting_response with slots filled for partial-fill cascade.
* run_due_regenerations: picks up only steps whose debounce timer has
  elapsed AND state is not terminal.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.app.models import (
    ActionPlan,
    ActionStep,
    StepResponse,
    Tenant,
)
from backend.app.services.action_plan.engine import (
    ActionPlanEngine,
    REGEN_DEBOUNCE_SECONDS,
    TERMINAL_STATES,
    run_due_regenerations,
)


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def two_step_plan(test_session_factory):
    """A->B plan. Upstream A produces slot 'X'; downstream B consumes it.
    Returns dict with session_factory + ids."""
    async with test_session_factory() as session:
        tenant = Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        plan = ActionPlan(
            tenant_id=tenant.id,
            domain="generic",
            status="active",
            procedures_applied=[],
            external_context_snapshot={},
        )
        session.add(plan)
        await session.flush()
        upstream = ActionStep(
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Upstream A",
            state="ready",
            depends_on=[],
            input_slots=[],
            output_schema=[{"slot_key": "X", "description": "X", "required": True}],
            output_data={},
            participants=[],
            prep_artifacts=[],
            role_in_plan="preparation",
        )
        session.add(upstream)
        await session.flush()
        downstream = ActionStep(
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Downstream B",
            state="blocked",
            depends_on=[str(upstream.id)],
            input_slots=[
                {
                    "slot_key": "X",
                    "description": "needs X from upstream",
                    "required": True,
                    "filled_by_step_id": str(upstream.id),
                    "filled_value": None,
                    "filled_at": None,
                }
            ],
            output_schema=[],
            output_data={},
            participants=[],
            prep_artifacts=[],
            role_in_plan="customer_endpoint",
            artifact_version=1,
            artifact_stale=False,
        )
        session.add(downstream)
        await session.commit()
        return {
            "session_factory": test_session_factory,
            "tenant_id": tenant.id,
            "plan_id": plan.id,
            "upstream_id": upstream.id,
            "downstream_id": downstream.id,
        }


# ──────────────────────────────────────────────────────────
# complete_step
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_step_marks_done_and_stamps_completed_at(two_step_plan):
    factory = two_step_plan["session_factory"]
    engine = ActionPlanEngine()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        await engine.complete_step(db, step=upstream, output_data={"X": "the value"})
        await db.commit()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        assert upstream.state == "done"
        assert upstream.completed_at is not None
        assert upstream.output_data == {"X": "the value"}


@pytest.mark.asyncio
async def test_complete_step_propagates_to_downstream_input_slot(two_step_plan):
    factory = two_step_plan["session_factory"]
    engine = ActionPlanEngine()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        await engine.complete_step(db, step=upstream, output_data={"X": "the value"})
        await db.commit()
    async with factory() as db:
        downstream = await db.get(ActionStep, two_step_plan["downstream_id"])
        assert downstream.state == "ready"  # unblocked
        slot = downstream.input_slots[0]
        assert slot["filled_value"] == "the value"
        assert slot["filled_at"] is not None
        assert downstream.artifact_stale is True
        assert downstream.regen_debounce_until is not None


@pytest.mark.asyncio
async def test_complete_step_idempotent_on_terminal_state(two_step_plan):
    factory = two_step_plan["session_factory"]
    engine = ActionPlanEngine()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        upstream.state = "done"
        await db.commit()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        affected = await engine.complete_step(db, step=upstream, output_data={"X": "later"})
        await db.commit()
    # No-op for terminal states.
    assert affected == []


@pytest.mark.asyncio
async def test_complete_step_completes_plan_when_all_steps_terminal(two_step_plan):
    factory = two_step_plan["session_factory"]
    engine = ActionPlanEngine()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        await engine.complete_step(db, step=upstream, output_data={"X": "v"})
        downstream = await db.get(ActionStep, two_step_plan["downstream_id"])
        await engine.complete_step(db, step=downstream)
        await db.commit()
    async with factory() as db:
        plan = await db.get(ActionPlan, two_step_plan["plan_id"])
        assert plan.status == "completed"
        assert plan.completed_at is not None


# ──────────────────────────────────────────────────────────
# skip_step
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_step_unblocks_downstream_without_filling_slot(two_step_plan):
    """Skipped upstream should mark downstream ready (skipped counts as
    done for unblocking) but leave the input_slot value None — the
    regenerated artifact will render the placeholder."""
    factory = two_step_plan["session_factory"]
    engine = ActionPlanEngine()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        await engine.skip_step(db, step=upstream, reason="not relevant")
        await db.commit()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        downstream = await db.get(ActionStep, two_step_plan["downstream_id"])
        assert upstream.state == "skipped"
        assert upstream.skip_reason == "not relevant"
        assert downstream.state == "ready"
        # Slot value stays None — the agent will see the placeholder.
        assert downstream.input_slots[0]["filled_value"] is None
        # But the artifact still goes stale so it regenerates with the
        # missing-slot placeholder visible.
        assert downstream.artifact_stale is True


# ──────────────────────────────────────────────────────────
# delete_step
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_step_strips_downstream_dependency_and_slot_pointer(
    two_step_plan,
):
    factory = two_step_plan["session_factory"]
    engine = ActionPlanEngine()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        await engine.delete_step(db, step=upstream)
        await db.commit()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        downstream = await db.get(ActionStep, two_step_plan["downstream_id"])
        assert upstream.state == "deleted"
        # Downstream's depends_on no longer references the deleted step.
        assert str(upstream.id) not in (downstream.depends_on or [])
        # Slot pointer is cleared.
        slot = downstream.input_slots[0]
        assert slot["filled_by_step_id"] is None
        assert slot["filled_value"] is None
        # Downstream is now ready (no remaining deps).
        assert downstream.state == "ready"
        # Marked stale because the input shape changed.
        assert downstream.artifact_stale is True


# ──────────────────────────────────────────────────────────
# apply_response
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_response_with_full_fill_completes_step(two_step_plan):
    factory = two_step_plan["session_factory"]
    engine = ActionPlanEngine()
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        response = StepResponse(
            step_id=upstream.id,
            tenant_id=upstream.tenant_id,
            source="manual_note",
            note_text="X is the value",
            extracted_data={"X": "the value"},
        )
        db.add(response)
        await db.flush()
        affected = await engine.apply_response(db, step=upstream, response=response)
        await db.commit()
    assert len(affected) == 1  # downstream
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        downstream = await db.get(ActionStep, two_step_plan["downstream_id"])
        assert upstream.state == "done"
        assert downstream.state == "ready"
        assert downstream.input_slots[0]["filled_value"] == "the value"


@pytest.mark.asyncio
async def test_apply_response_with_partial_fill_leaves_step_awaiting(
    test_session_factory,
):
    """Step with TWO required output slots, response fills only one ->
    step remains awaiting_response; downstream still gets partial fill."""
    async with test_session_factory() as session:
        tenant = Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        plan = ActionPlan(
            tenant_id=tenant.id,
            domain="generic",
            status="active",
            procedures_applied=[],
            external_context_snapshot={},
        )
        session.add(plan)
        await session.flush()
        step = ActionStep(
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="Two-slot step",
            state="awaiting_response",
            depends_on=[],
            input_slots=[],
            output_schema=[
                {"slot_key": "X", "description": "X", "required": True},
                {"slot_key": "Y", "description": "Y", "required": True},
            ],
            output_data={},
            participants=[],
            prep_artifacts=[],
            role_in_plan="preparation",
        )
        session.add(step)
        await session.commit()
        step_id = step.id
        tenant_id = tenant.id

    engine = ActionPlanEngine()
    async with test_session_factory() as db:
        step = await db.get(ActionStep, step_id)
        response = StepResponse(
            step_id=step.id,
            tenant_id=tenant_id,
            source="manual_note",
            note_text="only X",
            extracted_data={"X": "got it"},
        )
        db.add(response)
        await db.flush()
        await engine.apply_response(db, step=step, response=response)
        await db.commit()

    async with test_session_factory() as db:
        step = await db.get(ActionStep, step_id)
        assert step.state == "awaiting_response"
        assert step.output_data == {"X": "got it"}


# ──────────────────────────────────────────────────────────
# Debounce + run_due_regenerations
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debounce_until_set_when_artifact_marked_stale(two_step_plan):
    """The regen_debounce_until timestamp must be roughly
    now + REGEN_DEBOUNCE_SECONDS after a completion cascade."""
    factory = two_step_plan["session_factory"]
    engine = ActionPlanEngine()
    before = datetime.now(timezone.utc)
    async with factory() as db:
        upstream = await db.get(ActionStep, two_step_plan["upstream_id"])
        await engine.complete_step(db, step=upstream, output_data={"X": "v"})
        await db.commit()
    after = datetime.now(timezone.utc)
    async with factory() as db:
        downstream = await db.get(ActionStep, two_step_plan["downstream_id"])
        assert downstream.regen_debounce_until is not None
        scheduled = downstream.regen_debounce_until
        # Allow for SQLite tz-naive datetimes by normalizing.
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        expected_min = before + timedelta(seconds=REGEN_DEBOUNCE_SECONDS - 2)
        expected_max = after + timedelta(seconds=REGEN_DEBOUNCE_SECONDS + 2)
        assert expected_min <= scheduled <= expected_max


@pytest.mark.asyncio
async def test_run_due_regenerations_picks_only_due_and_non_terminal_steps(
    test_session_factory,
):
    """Only stale steps whose debounce window has elapsed AND that are
    in a non-terminal state should be returned. Done / skipped /
    deleted steps don't regenerate even if marked stale."""
    now = datetime.now(timezone.utc)
    past = now - timedelta(seconds=10)
    future = now + timedelta(seconds=600)

    async with test_session_factory() as session:
        tenant = Tenant(name="t", slug=f"t-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        plan = ActionPlan(
            tenant_id=tenant.id,
            domain="generic",
            status="active",
            procedures_applied=[],
            external_context_snapshot={},
        )
        session.add(plan)
        await session.flush()

        # Step 1: due + ready -> should be picked.
        ready_due = ActionStep(
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="ready due",
            state="ready",
            depends_on=[],
            input_slots=[],
            output_schema=[],
            output_data={},
            participants=[],
            prep_artifacts=[],
            role_in_plan="preparation",
            artifact_stale=True,
            regen_debounce_until=past,
        )
        # Step 2: due + done -> should NOT be picked.
        done_due = ActionStep(
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="done due",
            state="done",
            depends_on=[],
            input_slots=[],
            output_schema=[],
            output_data={},
            participants=[],
            prep_artifacts=[],
            role_in_plan="preparation",
            artifact_stale=True,
            regen_debounce_until=past,
        )
        # Step 3: not yet due + ready -> should NOT be picked.
        ready_future = ActionStep(
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="ready future",
            state="ready",
            depends_on=[],
            input_slots=[],
            output_schema=[],
            output_data={},
            participants=[],
            prep_artifacts=[],
            role_in_plan="preparation",
            artifact_stale=True,
            regen_debounce_until=future,
        )
        # Step 4: not stale -> should NOT be picked.
        not_stale = ActionStep(
            plan_id=plan.id,
            tenant_id=tenant.id,
            title="fresh",
            state="ready",
            depends_on=[],
            input_slots=[],
            output_schema=[],
            output_data={},
            participants=[],
            prep_artifacts=[],
            role_in_plan="preparation",
            artifact_stale=False,
            regen_debounce_until=past,
        )
        for s in (ready_due, done_due, ready_future, not_stale):
            session.add(s)
        await session.commit()

    # Patch ``_regenerate_steps_for_plan`` so we don't issue real LLM
    # calls — we just count which steps would be sent. Engine's
    # public scheduler entry point is run_due_regenerations.
    from backend.app.services.action_plan import engine as engine_mod

    received_step_ids = []

    async def _fake_regen(db, *, plan, tenant, steps):
        received_step_ids.extend([s.id for s in steps])
        for s in steps:
            s.artifact_stale = False
            s.regen_debounce_until = None
        return len(steps)

    orig = engine_mod._regenerate_steps_for_plan
    engine_mod._regenerate_steps_for_plan = _fake_regen
    try:
        async with test_session_factory() as db:
            count = await run_due_regenerations(db, limit=100)
            await db.commit()
    finally:
        engine_mod._regenerate_steps_for_plan = orig

    # Only step 1 (ready + due) is regenerated.
    assert count == 1
    async with test_session_factory() as db:
        rows = (
            await db.execute(
                select(ActionStep).where(ActionStep.title.in_(
                    ("ready due", "done due", "ready future", "fresh")
                ))
            )
        ).scalars().all()
        by_title = {s.title: s for s in rows}
        assert by_title["ready due"].artifact_stale is False
        assert by_title["done due"].artifact_stale is True  # untouched
        assert by_title["ready future"].artifact_stale is True
        assert by_title["fresh"].artifact_stale is False


# ──────────────────────────────────────────────────────────
# Constants — guardrails so they don't drift silently
# ──────────────────────────────────────────────────────────


def test_terminal_states_locked():
    assert TERMINAL_STATES == frozenset({"done", "skipped", "deleted"})


def test_regen_debounce_seconds_locked_at_30():
    assert REGEN_DEBOUNCE_SECONDS == 30
