"""Governed auto-executor — safety-first tests.

Covers the non-negotiable contract from the executor's docstring:

* default settings (global flag off, no per-tenant policy) dispatch
  NOTHING even when ready, fully-slotted steps exist — the provable
  no-op test;
* absent a policy row for a step's action_class, the tenant default is
  'manual' even when the global flag is on;
* shadow mode logs + writes an audit ``StepResponse`` but performs no
  external side effect and leaves the step state untouched;
* auto mode (send path mocked via the same ``_build_sender`` seam
  ``tests/test_emails.py`` uses) dispatches once, and a retry/redeploy
  (simulated by forcing the step back to 'ready') does not re-send
  thanks to the #164 exactly-once ledger;
* unfilled slots are skipped;
* the per-tenant rate cap stops a run early;
* approve_then_auto moves the step to pending_approval and stops.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.models import (
    ActionPlan,
    ActionStep,
    AutoExecutionPolicy,
    Contact,
    Customer,
    EmailSend,
    Integration,
    Interaction,
    StepArtifact,
    StepResponse,
    Tenant,
)
from backend.app.services.action_plan.executor import run_due_executions

pytestmark = pytest.mark.asyncio


@pytest.fixture
def auto_execution_enabled(monkeypatch):
    # get_settings() is cached; flip the flag on the live instance (same
    # pattern as tests/test_sso_jit.py's jit_enabled fixture).
    monkeypatch.setattr(get_settings(), "AUTO_EXECUTION_ENABLED", True)


class _CountingSender:
    """Stands in for GmailSender via a patched _build_sender, counting
    calls so tests can assert a mocked provider was (or wasn't) hit."""

    def __init__(self, outcome: Any):
        self.outcome = outcome
        self.send_calls = 0
        self.closed = 0

    async def send(self, **kwargs):
        self.send_calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def close(self):
        self.closed += 1


def _patch_sender(monkeypatch, outcome: Any) -> _CountingSender:
    from backend.app.api import emails as emails_module

    sender = _CountingSender(outcome)
    monkeypatch.setattr(
        emails_module, "_build_sender", lambda integ, principal_email_hint: sender
    )
    return sender


def _forbid_sender(monkeypatch) -> None:
    """Patch _build_sender to blow up if called at all — proves the
    executor never even reaches the provider for this scenario."""
    from backend.app.api import emails as emails_module

    def _boom(*args, **kwargs):
        raise AssertionError(
            "dispatch must not build a sender in this scenario"
        )

    monkeypatch.setattr(emails_module, "_build_sender", _boom)


async def _seed(
    session_factory,
    *,
    n_steps: int = 1,
    mode: Optional[str] = None,
    action_class: str = "high_risk",
    awaits_response: bool = False,
    unfilled_slots: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Tenant + connected Gmail integration + resolvable customer contact
    + an active plan with ``n_steps`` 'ready' email steps, each with a
    fully-slotted artifact. Optionally seeds an AutoExecutionPolicy row
    for (tenant, action_class)."""
    async with session_factory() as db:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()

        integ = Integration(
            tenant_id=tenant.id, provider="google",
            access_token=None, refresh_token=None,
        )
        customer = Customer(tenant_id=tenant.id, name="Big Co")
        db.add_all([integ, customer])
        await db.flush()

        contact = Contact(
            tenant_id=tenant.id, customer_id=customer.id,
            name="Jane Customer", email="jane@bigco.com",
        )
        db.add(contact)
        interaction = Interaction(
            tenant_id=tenant.id, channel="voice",
            customer_id=customer.id, contact_id=contact.id,
        )
        db.add(interaction)
        await db.flush()

        plan = ActionPlan(
            tenant_id=tenant.id, interaction_id=interaction.id, domain="generic",
            status="active", procedures_applied=[], external_context_snapshot={},
        )
        db.add(plan)
        await db.flush()

        steps = []
        for i in range(n_steps):
            step = ActionStep(
                plan_id=plan.id, tenant_id=tenant.id,
                title=f"Follow up with Jane #{i}",
                state="ready", depends_on=[], input_slots=[], output_schema=[],
                output_data={},
                participants=[{"name": "Jane Customer", "side": "customer"}],
                prep_artifacts=[], role_in_plan="customer_endpoint",
                recommended_channel="email", awaits_response=awaits_response,
            )
            db.add(step)
            await db.flush()
            artifact = StepArtifact(
                step_id=step.id, tenant_id=tenant.id, version=1, kind="email",
                payload={
                    "subject": "Following up",
                    "body": "Hi Jane, following up on our call.",
                    "unfilled_slots": unfilled_slots or [],
                },
            )
            db.add(artifact)
            steps.append(step)

        if mode is not None:
            db.add(AutoExecutionPolicy(
                tenant_id=tenant.id, action_class=action_class, mode=mode,
            ))

        await db.commit()
        for s in steps:
            await db.refresh(s)
        await db.refresh(tenant)
        return {"tenant": tenant, "plan": plan, "steps": steps, "step": steps[0]}


# ──────────────────────────────────────────────────────────
# The safety test
# ──────────────────────────────────────────────────────────


async def test_default_off_dispatches_nothing_even_with_an_auto_policy(
    test_session_factory, monkeypatch,
):
    """Global flag OFF (the shipped default) — even with an explicit
    'auto' policy already on file and a ready, fully-slotted step, the
    executor is a provable no-op: no sender is ever touched, no state
    change, no audit row."""
    seed = await _seed(test_session_factory, mode="auto")
    tenant = seed["tenant"]
    step = seed["step"]
    _forbid_sender(monkeypatch)

    assert get_settings().AUTO_EXECUTION_ENABLED is False

    async with test_session_factory() as db:
        result = await run_due_executions(db, tenant_id=tenant.id)

    assert result == {"enabled": False, "reason": "AUTO_EXECUTION_ENABLED is False"}

    async with test_session_factory() as db:
        refreshed = await db.get(ActionStep, step.id)
        assert refreshed.state == "ready"
        responses = (
            await db.execute(select(StepResponse).where(StepResponse.step_id == step.id))
        ).scalars().all()
        assert responses == []
        sends = (
            await db.execute(select(EmailSend).where(EmailSend.tenant_id == tenant.id))
        ).scalars().all()
        assert sends == []


async def test_no_policy_row_defaults_to_manual_even_when_flag_is_on(
    auto_execution_enabled, test_session_factory, monkeypatch,
):
    """Global flag ON, but no AutoExecutionPolicy row at all: absent =
    'manual', so still nothing dispatches."""
    seed = await _seed(test_session_factory, mode=None)
    tenant = seed["tenant"]
    step = seed["step"]
    _forbid_sender(monkeypatch)

    async with test_session_factory() as db:
        result = await run_due_executions(db, tenant_id=tenant.id)

    assert result["dispatched"] == 0
    assert result["skipped_no_policy"] == 1

    async with test_session_factory() as db:
        refreshed = await db.get(ActionStep, step.id)
        assert refreshed.state == "ready"


# ──────────────────────────────────────────────────────────
# Shadow mode
# ──────────────────────────────────────────────────────────


async def test_shadow_mode_audits_but_performs_no_side_effect(
    auto_execution_enabled, test_session_factory, monkeypatch,
):
    seed = await _seed(test_session_factory, mode="shadow")
    tenant = seed["tenant"]
    step = seed["step"]
    _forbid_sender(monkeypatch)

    async with test_session_factory() as db:
        result = await run_due_executions(db, tenant_id=tenant.id)

    assert result["shadow_logged"] == 1
    assert result["dispatched"] == 0

    async with test_session_factory() as db:
        refreshed = await db.get(ActionStep, step.id)
        assert refreshed.state == "ready"  # untouched
        responses = (
            await db.execute(select(StepResponse).where(StepResponse.step_id == step.id))
        ).scalars().all()
        assert len(responses) == 1
        assert responses[0].source == "auto_executed"
        assert responses[0].extracted_data["mode"] == "shadow"
        assert responses[0].extracted_data["action_class"] == "high_risk"
        sends = (
            await db.execute(select(EmailSend).where(EmailSend.tenant_id == tenant.id))
        ).scalars().all()
        assert sends == []


# ──────────────────────────────────────────────────────────
# Auto mode: dispatch + idempotency + unfilled-slot skip + rate cap
# ──────────────────────────────────────────────────────────


async def test_auto_mode_dispatches_once_and_is_idempotent_on_retry(
    auto_execution_enabled, test_session_factory, monkeypatch,
):
    seed = await _seed(test_session_factory, mode="auto")
    tenant = seed["tenant"]
    step = seed["step"]
    sender = _patch_sender(
        monkeypatch, SimpleNamespace(provider_message_id="prov-1", message_id="msg-1"),
    )

    async with test_session_factory() as db:
        result = await run_due_executions(db, tenant_id=tenant.id)

    assert result["dispatched"] == 1
    assert sender.send_calls == 1

    async with test_session_factory() as db:
        refreshed = await db.get(ActionStep, step.id)
        assert refreshed.state == "done"  # awaits_response=False -> done
        responses = (
            await db.execute(select(StepResponse).where(StepResponse.step_id == step.id))
        ).scalars().all()
        sources = {r.source for r in responses}
        assert "auto_executed" in sources
        assert "outbound_email_sent" in sources
        # Simulate a retry/redeploy race: something forces the step back
        # to 'ready' (e.g. a concurrent duplicate tick reading stale
        # state) so the next tick's query would pick it up again absent
        # the ledger guard.
        refreshed.state = "ready"
        await db.commit()

    async with test_session_factory() as db:
        result2 = await run_due_executions(db, tenant_id=tenant.id)

    assert result2["dispatched"] == 0
    assert result2["skipped_already_dispatched"] == 1
    assert sender.send_calls == 1  # never called a second time

    async with test_session_factory() as db:
        sends = (
            await db.execute(select(EmailSend).where(EmailSend.tenant_id == tenant.id))
        ).scalars().all()
        assert len(sends) == 1


async def test_unfilled_slots_are_skipped(
    auto_execution_enabled, test_session_factory, monkeypatch,
):
    seed = await _seed(test_session_factory, mode="auto", unfilled_slots=["customer_name"])
    tenant = seed["tenant"]
    _forbid_sender(monkeypatch)

    async with test_session_factory() as db:
        result = await run_due_executions(db, tenant_id=tenant.id)

    assert result["dispatched"] == 0
    assert result["skipped_unfilled_slots"] == 1


async def test_rate_cap_stops_a_run_early(
    auto_execution_enabled, test_session_factory, monkeypatch,
):
    monkeypatch.setattr(get_settings(), "AUTO_EXECUTION_MAX_DISPATCHES_PER_TENANT", 1)
    seed = await _seed(test_session_factory, n_steps=3, mode="shadow")
    tenant = seed["tenant"]
    _forbid_sender(monkeypatch)

    async with test_session_factory() as db:
        result = await run_due_executions(db, tenant_id=tenant.id)

    assert result["shadow_logged"] == 1
    assert result["skipped_rate_capped"] == 1


# ──────────────────────────────────────────────────────────
# approve_then_auto
# ──────────────────────────────────────────────────────────


async def test_approve_then_auto_moves_to_pending_approval_and_stops(
    auto_execution_enabled, test_session_factory, monkeypatch,
):
    seed = await _seed(test_session_factory, mode="approve_then_auto")
    tenant = seed["tenant"]
    step = seed["step"]
    _forbid_sender(monkeypatch)

    async with test_session_factory() as db:
        result = await run_due_executions(db, tenant_id=tenant.id)

    assert result["pending_approval"] == 1
    assert result["dispatched"] == 0

    async with test_session_factory() as db:
        refreshed = await db.get(ActionStep, step.id)
        assert refreshed.state == "pending_approval"
        responses = (
            await db.execute(select(StepResponse).where(StepResponse.step_id == step.id))
        ).scalars().all()
        assert len(responses) == 1
        assert responses[0].source == "auto_executed"
        assert responses[0].extracted_data["mode"] == "approve_then_auto"
