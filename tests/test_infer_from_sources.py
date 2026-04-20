"""Unit tests for the Infer-From-Sources agent."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.kb.infer_from_sources import (
    InferFromSources,
    SuggestionOut,
    _coerce_suggestions,
    _is_redundant,
    _suggestion_key,
)


def test_coerce_drops_bad_sections_and_low_confidence():
    raw = [
        {"section": "goals", "proposed_value": ["grow"], "confidence": 0.8, "rationale": "r1"},
        {"section": "unknown", "proposed_value": "x", "confidence": 0.9},
        {"section": "strategies", "proposed_value": "y", "confidence": 0.1},  # too low
        {"section": "strategies", "proposed_value": "", "confidence": 0.9},   # empty
        "bad shape",
    ]
    out = _coerce_suggestions(raw, evidence_refs={"interaction_ids": ["abc"]})
    assert len(out) == 1
    assert out[0].section == "goals"
    assert out[0].confidence == 0.8
    assert "interaction:abc" in out[0].evidence_refs


def test_coerce_caps_at_eight():
    raw = [
        {"section": "goals", "proposed_value": f"g{i}", "confidence": 0.7}
        for i in range(20)
    ]
    out = _coerce_suggestions(raw, evidence_refs={})
    assert len(out) == 8


def test_is_redundant_exact_string_in_list():
    current = {"goals": ["existing goal"]}
    s = SuggestionOut(
        section="goals",
        path=None,
        proposed_value="existing goal",
        rationale="",
        confidence=0.9,
        evidence_refs=[],
    )
    assert _is_redundant(s, current) is True


def test_is_redundant_new_string_not_redundant():
    current = {"goals": ["existing goal"]}
    s = SuggestionOut(
        section="goals",
        path=None,
        proposed_value="new goal",
        rationale="",
        confidence=0.9,
        evidence_refs=[],
    )
    assert _is_redundant(s, current) is False


def test_is_redundant_kpi_name_match():
    current = {"kpis": [{"name": "CSAT", "target": 4.5}]}
    s = SuggestionOut(
        section="kpis",
        path=None,
        proposed_value=[{"name": "CSAT", "target": 4.7}],  # same name, different target
        rationale="",
        confidence=0.9,
        evidence_refs=[],
    )
    assert _is_redundant(s, current) is True


def test_is_redundant_nested_path():
    current = {"personal_touches": {"greeting_style": "Warm"}}
    s = SuggestionOut(
        section="personal_touches",
        path="personal_touches.greeting_style",
        proposed_value="Warm",
        rationale="",
        confidence=0.9,
        evidence_refs=[],
    )
    assert _is_redundant(s, current) is True


def test_suggestion_key_is_stable_for_dedupe():
    k1 = _suggestion_key("goals", None, ["a", "b"])
    k2 = _suggestion_key("goals", None, ["a", "b"])
    k3 = _suggestion_key("goals", None, ["a", "c"])
    assert k1 == k2
    assert k1 != k3


@pytest.mark.asyncio
async def test_call_haiku_parses_suggestion_list():
    fake = SimpleNamespace(
        content=[
            SimpleNamespace(
                text=json.dumps(
                    {
                        "suggestions": [
                            {
                                "section": "strategies",
                                "proposed_value": "Lead with ROI for enterprise",
                                "rationale": "seen in 4/5 wins",
                                "confidence": 0.8,
                            }
                        ]
                    }
                )
            )
        ]
    )
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake)
    agent = InferFromSources(client=client)

    out = await agent._call_haiku(
        {
            "current_fields": {},
            "playbook": {},
            "activity_summary": {"outcome_counts": {"closed_won": 4}},
            "snippets": ["[closed_won] summary", "[closed_won] summary"],
        }
    )
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["section"] == "strategies"


@pytest.mark.asyncio
async def test_call_haiku_empty_on_bad_json():
    fake = SimpleNamespace(content=[SimpleNamespace(text="not json")])
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake)
    agent = InferFromSources(client=client)
    out = await agent._call_haiku(
        {"current_fields": {}, "playbook": {}, "activity_summary": {}, "snippets": []}
    )
    assert out == []
