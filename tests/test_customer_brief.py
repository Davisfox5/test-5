"""Unit tests for the CustomerBriefBuilder formatter + validator."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.kb.customer_brief_builder import (
    CustomerBriefBuilder,
    _empty_brief,
    _validate_brief,
    format_customer_brief_for_prompt,
)


def test_format_empty_returns_empty_string():
    assert format_customer_brief_for_prompt({}) == ""
    assert format_customer_brief_for_prompt(_empty_brief()) == ""


def test_format_renders_expected_sections():
    brief = {
        "current_status": "at_risk",
        "overview": "Mid-market SaaS account evaluating the Pro plan.",
        "stakeholders": [
            {"name": "Sarah Lee", "role": "CFO", "preferences": "Data-driven, impatient"}
        ],
        "interests": ["ROI analysis", "SSO"],
        "objections_raised": [
            {"objection": "Too expensive", "context": "vs. BasicCall", "resolved": False}
        ],
        "preferences": "Direct, short answers.",
        "best_approaches": ["Lead with ROI numbers"],
        "avoid": ["Long demos"],
        "churn_signals": ["3 open tickets > 14 days old"],
        "upsell_signals": [],
        "timeline": [{"when": "2026-04-10", "note": "Escalated ticket #42"}],
    }
    out = format_customer_brief_for_prompt(brief)
    assert "# Customer context" in out
    assert "Status: at_risk" in out
    assert "Sarah Lee" in out
    assert "ROI analysis" in out
    assert "Too expensive" in out
    assert "3 open tickets" in out


def test_validate_tolerates_partial_input():
    partial = {"overview": "hi", "best_approaches": ["one"], "extra": "ignored"}
    out = _validate_brief(partial)
    assert out["overview"] == "hi"
    assert out["best_approaches"] == ["one"]
    assert out["churn_signals"] == []
    assert out["stakeholders"] == []


def test_validate_non_dict_returns_empty():
    assert _validate_brief("nope") == _empty_brief()
    assert _validate_brief(None) == _empty_brief()


@pytest.mark.asyncio
async def test_call_haiku_parses_returned_json():
    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                text=json.dumps(
                    {
                        "current_status": "champion",
                        "overview": "Long-time customer on Enterprise.",
                        "best_approaches": ["Always involve their CFO"],
                        "avoid": ["Pushy closes"],
                    }
                )
            )
        ]
    )
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake_response)
    builder = CustomerBriefBuilder(client=client)

    evidence = {
        "customer": {"name": "Acme"},
        "contacts": [{"name": "Sarah"}],
        "interaction_blocks": ["[2026-04-10] voice / outcome=closed_won"],
        "events": [{"event_type": "upsold"}],
    }
    out = await builder._call_haiku(evidence)
    assert out["current_status"] == "champion"
    assert out["best_approaches"] == ["Always involve their CFO"]
    # Ensure the evidence gets into the prompt.
    call_kwargs = client.messages.create.await_args.kwargs
    assert "Sarah" in call_kwargs["messages"][0]["content"]
    assert "upsold" in call_kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_call_haiku_returns_empty_on_bad_json():
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(text="not a json body at all")]
    )
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake_response)
    builder = CustomerBriefBuilder(client=client)

    evidence = {
        "customer": {"name": "x"},
        "contacts": [],
        "interaction_blocks": [],
        "events": [],
    }
    out = await builder._call_haiku(evidence)
    assert out == _empty_brief()
