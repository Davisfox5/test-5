"""Extractor helpers — strip_quoted_reply + the matcher's RFC 822 logic.

These tests cover the pure-logic helpers (no LLM call). The Call D
extraction itself is exercised indirectly via the engine tests that
inject a fake ResponseExtractor.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from backend.app.models import ActionPlan, ActionStep, StepResponse, Tenant
from backend.app.services.action_plan.extractor import (
    match_inbound_email,
    strip_quoted_reply,
)


# ──────────────────────────────────────────────────────────
# strip_quoted_reply — deterministic; covers the common patterns
# Gmail / Outlook insert below a reply.
# ──────────────────────────────────────────────────────────


def test_strip_quoted_reply_empty_returns_empty():
    assert strip_quoted_reply("") == ""


def test_strip_quoted_reply_no_quotes_passes_through():
    body = "Just the new content,\nno history at all."
    assert strip_quoted_reply(body) == body


def test_strip_quoted_reply_strips_on_wrote_marker():
    body = (
        "New content here.\n"
        "More new content.\n"
        "\n"
        "On Mon, May 25, 2026, Kevin wrote:\n"
        "> Old quoted line 1\n"
        "> Old quoted line 2\n"
    )
    out = strip_quoted_reply(body)
    assert "New content here." in out
    assert "More new content." in out
    assert "Kevin wrote" not in out
    assert "Old quoted line" not in out


def test_strip_quoted_reply_strips_on_outlook_original_message():
    body = (
        "Quick yes.\n"
        "-------- Original Message --------\n"
        "From: someone@example.com\n"
        "Subject: re: foo\n"
        "Older content below\n"
    )
    out = strip_quoted_reply(body)
    assert out.startswith("Quick yes.")
    assert "Original Message" not in out


def test_strip_quoted_reply_strips_on_three_consecutive_quote_lines():
    body = (
        "Hi all,\n"
        "Just confirming.\n"
        "\n"
        "> first quoted\n"
        "> second quoted\n"
        "> third quoted\n"
        "> fourth quoted\n"
    )
    out = strip_quoted_reply(body)
    assert "Just confirming." in out
    assert "third quoted" not in out
    assert "fourth quoted" not in out


def test_strip_quoted_reply_never_returns_empty_for_nonempty_input():
    """Conservative guarantee — a buggy heuristic that strips
    everything must not return an empty string. We'd rather feed Call D
    a too-large body than nothing at all."""
    body = "> entirely quoted\n> all of it\n> no new content"
    out = strip_quoted_reply(body)
    assert out  # non-empty


# ──────────────────────────────────────────────────────────
# match_inbound_email — RFC 822 chain walk; In-Reply-To wins; falls
# through to References; no match returns reason='no_match'; closed
# step returns reason='step_closed'.
# ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def _seeded_plan(test_session_factory):
    """A tenant + plan + step + outbound StepResponse with a known
    message id. Returns (tenant_id, step_id, outbound_msg_id)."""
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
            title="Email the customer",
            state="awaiting_response",
            depends_on=[],
            input_slots=[],
            output_schema=[],
            output_data={},
            role_in_plan="customer_endpoint",
            participants=[],
            prep_artifacts=[],
        )
        session.add(step)
        await session.flush()
        outbound = StepResponse(
            step_id=step.id,
            tenant_id=tenant.id,
            source="outbound_email_sent",
            outbound_message_id="<sent-123@vendor.example>",
        )
        session.add(outbound)
        await session.commit()
        return {
            "tenant_id": tenant.id,
            "step_id": step.id,
            "outbound_message_id": outbound.outbound_message_id,
            "session_factory": test_session_factory,
        }


@pytest.mark.asyncio
async def test_match_inbound_email_matches_via_in_reply_to(_seeded_plan):
    async with _seeded_plan["session_factory"]() as db:
        result = await match_inbound_email(
            db,
            tenant_id=_seeded_plan["tenant_id"],
            in_reply_to="<sent-123@vendor.example>",
            references=[],
        )
    assert result.step_id == _seeded_plan["step_id"]
    assert result.reason == "in_reply_to"


@pytest.mark.asyncio
async def test_match_inbound_email_walks_references_chain(_seeded_plan):
    async with _seeded_plan["session_factory"]() as db:
        result = await match_inbound_email(
            db,
            tenant_id=_seeded_plan["tenant_id"],
            in_reply_to=None,
            references=[
                "<unrelated-1@example.com>",
                "<unrelated-2@example.com>",
                "<sent-123@vendor.example>",
            ],
        )
    assert result.step_id == _seeded_plan["step_id"]
    assert result.reason == "references"


@pytest.mark.asyncio
async def test_match_inbound_email_no_match_returns_no_match(_seeded_plan):
    """Locked decision: never guess. If neither header matches a known
    outbound, return reason='no_match' and let the agent route."""
    async with _seeded_plan["session_factory"]() as db:
        result = await match_inbound_email(
            db,
            tenant_id=_seeded_plan["tenant_id"],
            in_reply_to="<unknown@example.com>",
            references=["<also-unknown@example.com>"],
        )
    assert result.step_id is None
    assert result.reason == "no_match"


@pytest.mark.asyncio
async def test_match_inbound_email_empty_headers_returns_no_match(_seeded_plan):
    async with _seeded_plan["session_factory"]() as db:
        result = await match_inbound_email(
            db,
            tenant_id=_seeded_plan["tenant_id"],
            in_reply_to=None,
            references=[],
        )
    assert result.step_id is None
    assert result.reason == "no_match"


@pytest.mark.asyncio
async def test_match_inbound_email_closed_step_returns_step_closed(_seeded_plan):
    """If the matched step is done/skipped/deleted, the inbound is
    informational only — reason='step_closed', step_id still returned
    so the UI can show 'reply landed on a closed step'."""
    factory = _seeded_plan["session_factory"]
    async with factory() as db:
        step = await db.get(ActionStep, _seeded_plan["step_id"])
        step.state = "done"
        await db.commit()
    async with factory() as db:
        result = await match_inbound_email(
            db,
            tenant_id=_seeded_plan["tenant_id"],
            in_reply_to=_seeded_plan["outbound_message_id"],
            references=[],
        )
    assert result.step_id == _seeded_plan["step_id"]
    assert result.reason == "step_closed"


@pytest.mark.asyncio
async def test_match_inbound_email_in_reply_to_wins_when_both_match(
    _seeded_plan,
):
    """In-Reply-To beats References when both reference the same step;
    we surface that in the reason field so debugging is unambiguous."""
    async with _seeded_plan["session_factory"]() as db:
        result = await match_inbound_email(
            db,
            tenant_id=_seeded_plan["tenant_id"],
            in_reply_to=_seeded_plan["outbound_message_id"],
            references=[_seeded_plan["outbound_message_id"]],
        )
    assert result.step_id == _seeded_plan["step_id"]
    assert result.reason == "in_reply_to"
