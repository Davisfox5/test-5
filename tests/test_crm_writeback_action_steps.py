"""CRM write-back sources tasks from the Action Plan DAG (4b cutover).

The legacy path read ActionItem rows with status IN ('pending',
'in_progress') — values the pipeline never wrote — so activity write-back
was silently dark. These tests pin the new behavior: open ActionSteps of
the interaction's plan become CRM tasks; finished/skipped/deleted steps
and other interactions' plans do not.
"""

import uuid

import pytest

from backend.app.services.crm.writeback import _open_action_steps


@pytest.mark.asyncio
async def test_open_steps_returns_only_actionable_states(
    test_session, test_tenant, test_interaction
):
    from backend.app.models import ActionPlan, ActionStep

    plan = ActionPlan(
        tenant_id=test_tenant.id,
        interaction_id=test_interaction.id,
    )
    test_session.add(plan)
    await test_session.flush()

    states = [
        ("ready", True),
        ("blocked", True),
        ("in_progress", True),
        ("awaiting_response", True),
        ("done", False),
        ("skipped", False),
        ("deleted", False),
    ]
    for state, _expected in states:
        test_session.add(
            ActionStep(
                plan_id=plan.id,
                tenant_id=test_tenant.id,
                title="step-{0}".format(state),
                state=state,
            )
        )
    await test_session.commit()

    steps = await _open_action_steps(test_session, test_interaction.id)
    got = {s.title for s in steps}
    assert got == {
        "step-ready",
        "step-blocked",
        "step-in_progress",
        "step-awaiting_response",
    }


@pytest.mark.asyncio
async def test_open_steps_scoped_to_the_interaction(
    test_session, test_tenant, test_interaction
):
    from backend.app.models import ActionPlan, ActionStep, Interaction

    other_interaction = Interaction(tenant_id=test_tenant.id, channel="voice")
    test_session.add(other_interaction)
    await test_session.flush()

    other_plan = ActionPlan(
        tenant_id=test_tenant.id,
        interaction_id=other_interaction.id,
    )
    test_session.add(other_plan)
    await test_session.flush()
    test_session.add(
        ActionStep(
            plan_id=other_plan.id,
            tenant_id=test_tenant.id,
            title="someone-elses-step",
            state="ready",
        )
    )
    await test_session.commit()

    assert await _open_action_steps(test_session, test_interaction.id) == []
