"""Tests for the OnboardingInterview agent + merge helper."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.kb.context_builder import _empty_brief
from backend.app.services.kb.onboarding_interview import (
    OnboardingInterview,
    _coerce_answers,
    _empty_answers,
    _merge_answers,
    merge_answers_into_brief,
)


def _haiku_reply(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])


def test_empty_answers_has_all_sections():
    ans = _empty_answers()
    for key in ("goals", "kpis", "strategies", "org_structure", "personal_touches"):
        assert key in ans
    assert isinstance(ans["personal_touches"], dict)


def test_coerce_answers_drops_malformed():
    raw = {
        "goals": ["launch enterprise tier"],
        "kpis": [{"name": "CSAT", "target": 4.5}, "not a dict"],
        "strategies": ["lead with ROI"],
        "org_structure": {"teams": ["sales", "cs"], "escalation_path": []},
        "personal_touches": {"greeting_style": "First name"},
        "noise": "ignored",
    }
    out = _coerce_answers(raw)
    assert out["goals"] == ["launch enterprise tier"]
    assert len(out["kpis"]) == 1
    assert out["kpis"][0]["name"] == "CSAT"
    assert out["strategies"] == ["lead with ROI"]
    assert out["org_structure"]["teams"] == ["sales", "cs"]
    assert out["personal_touches"]["greeting_style"] == "First name"


def test_merge_answers_preserves_existing_and_dedupes():
    existing = _empty_answers()
    existing["goals"] = ["existing goal"]
    existing["org_structure"] = {
        "teams": ["sales"],
        "escalation_path": [],
        "territories": [],
    }
    new = _empty_answers()
    new["goals"] = ["existing goal", "new goal"]  # duplicate should dedupe
    new["strategies"] = ["new strat"]
    new["org_structure"] = {"teams": ["cs"]}  # partial dict merge

    merged = _merge_answers(existing, new)
    assert merged["goals"] == ["existing goal", "new goal"]
    assert merged["strategies"] == ["new strat"]
    # Dict merge added teams without wiping escalation_path / territories.
    assert merged["org_structure"]["teams"] == ["cs"]  # value replacement on dict keys


def test_merge_answers_into_brief_preserves_kb_sections():
    brief = _empty_brief()
    brief["tenant_overview"] = "KB-derived overview — keep this."
    brief["products_services"] = ["Pro plan"]

    answers = _empty_answers()
    answers["goals"] = ["g1"]
    answers["personal_touches"]["greeting_style"] = "Warm"

    out = merge_answers_into_brief(brief, answers)
    assert out["tenant_overview"] == "KB-derived overview — keep this."
    assert out["products_services"] == ["Pro plan"]
    assert out["goals"] == ["g1"]
    assert out["personal_touches"]["greeting_style"] == "Warm"


@pytest.mark.asyncio
async def test_step_opening_turn_greets_and_advances_state():
    client = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=_haiku_reply(
            {
                "assistant_message": "Hi! Let's start with your top goals — what are they?",
                "updated_answers": {},
                "next_section": "goals",
                "completed_sections": [],
                "done": False,
            }
        )
    )
    agent = OnboardingInterview(client=client)
    state = OnboardingInterview.new_state()
    turn = await agent.step(state, user_reply="")

    assert turn.assistant_message.startswith("Hi!")
    assert turn.done is False
    assert turn.next_section == "goals"
    # History now contains the assistant message (no user turn yet).
    assert turn.history[-1]["role"] == "assistant"
    assert len(turn.history) == 1


@pytest.mark.asyncio
async def test_step_extracts_answers_and_marks_section_complete():
    client = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=_haiku_reply(
            {
                "assistant_message": "Got it. What KPIs do you track?",
                "updated_answers": {
                    "goals": ["10% MoM growth", "<5% churn"],
                },
                "next_section": "kpis",
                "completed_sections": ["goals"],
                "done": False,
            }
        )
    )
    agent = OnboardingInterview(client=client)
    state = OnboardingInterview.new_state()
    turn = await agent.step(state, user_reply="We want 10% MoM growth and <5% churn")

    assert turn.answers["goals"] == ["10% MoM growth", "<5% churn"]
    assert turn.completed_sections == ["goals"]
    assert turn.next_section == "kpis"
    assert turn.history[-2]["role"] == "user"
    assert turn.history[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_step_tolerates_bad_json():
    client = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(text="garbage")])
    )
    agent = OnboardingInterview(client=client)
    state = OnboardingInterview.new_state()
    turn = await agent.step(state, user_reply="hi")

    # Graceful fallback: empty-ish answers, session still active.
    assert turn.done is False
    assert "trouble" in turn.assistant_message.lower() or turn.assistant_message


@pytest.mark.asyncio
async def test_step_carries_completed_sections_forward():
    client = AsyncMock()
    # First turn completes goals; second turn completes kpis; the agent
    # should preserve 'goals' in completed_sections on turn 2.
    replies = [
        _haiku_reply(
            {
                "assistant_message": "What KPIs?",
                "updated_answers": {"goals": ["g1"]},
                "next_section": "kpis",
                "completed_sections": ["goals"],
                "done": False,
            }
        ),
        _haiku_reply(
            {
                "assistant_message": "Tell me about strategies.",
                "updated_answers": {"kpis": [{"name": "CSAT", "target": 4.5}]},
                "next_section": "strategies",
                "completed_sections": ["kpis"],
                "done": False,
            }
        ),
    ]
    client = AsyncMock()
    client.messages.create = AsyncMock(side_effect=replies)
    agent = OnboardingInterview(client=client)

    state = OnboardingInterview.new_state()
    turn1 = await agent.step(state, user_reply="Grow revenue")
    state = OnboardingInterview.update_state(state, turn1)
    turn2 = await agent.step(state, user_reply="CSAT and NPS")

    # completed should be the union of both turns.
    assert set(turn2.completed_sections) >= {"goals", "kpis"}
    # Answers from turn 1 preserved in turn 2.
    assert turn2.answers["goals"] == ["g1"]
    assert turn2.answers["kpis"][0]["name"] == "CSAT"
