"""Unit tests for the TenantBriefRefiner summariser + formatter."""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.kb.tenant_brief_refiner import (
    TenantBriefRefiner,
    _empty_playbook,
    _summarise_interactions,
    _validate_playbook,
)


def _interaction(outcome_type: str, adherence: float = 80.0, summary: str = "call") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        outcome_type=outcome_type,
        created_at=datetime.now(timezone.utc),
        insights={
            "summary": summary,
            "coaching": {
                "script_adherence_score": adherence,
                "what_went_well": ["opened with ROI"],
                "improvements": ["needed better close"],
            },
            "competitor_mentions": [{"name": "BasicCall", "handled_well": True}],
        },
    )


def test_summarise_counts_wins_and_losses():
    interactions = [
        _interaction("closed_won"),
        _interaction("closed_won"),
        _interaction("demo_scheduled"),
        _interaction("closed_lost"),
        _interaction("unresolved"),
    ]
    out = _summarise_interactions(interactions, events=[])
    assert out["wins"] == 3  # closed_won x2 + demo_scheduled
    assert out["losses"] == 2  # closed_lost + unresolved
    assert out["by_outcome_type"]["closed_won"] == 2
    assert out["won_snippets"]  # non-empty


def test_validate_playbook_clips_and_coerces():
    raw = {
        "what_works": ["a"] * 20,  # over limit
        "winning_objection_handlers": [
            {"objection": "too expensive", "handler": "show ROI"},
            "malformed",
        ],
    }
    out = _validate_playbook(raw)
    assert len(out["what_works"]) <= 8
    assert len(out["winning_objection_handlers"]) == 1
    assert out["winning_objection_handlers"][0]["handler"] == "show ROI"
    assert out["what_doesnt"] == []  # missing key filled in


@pytest.mark.asyncio
async def test_call_haiku_parses_json():
    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                text=json.dumps(
                    {
                        "what_works": ["Lead with ROI"],
                        "what_doesnt": ["Long intros"],
                        "top_performing_phrases": ["return on investment"],
                        "common_failure_modes": ["Skipped discovery"],
                        "winning_objection_handlers": [
                            {"objection": "Too expensive", "handler": "Show 6-month payback"}
                        ],
                    }
                )
            )
        ]
    )
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake_response)
    refiner = TenantBriefRefiner(client=client)

    aggregates = {
        "by_outcome_type": {"closed_won": 5, "closed_lost": 2},
        "wins": 5,
        "losses": 2,
        "won_snippets": ["[closed_won; 85] stuff"],
        "lost_snippets": ["[closed_lost; 60] stuff"],
        "customer_events": [{"event_type": "became_customer"}],
    }
    out = await refiner._call_haiku(aggregates)
    assert out["what_works"] == ["Lead with ROI"]
    assert out["winning_objection_handlers"][0]["objection"] == "Too expensive"


@pytest.mark.asyncio
async def test_call_haiku_empty_on_bad_json():
    fake_response = SimpleNamespace(content=[SimpleNamespace(text="bogus")])
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake_response)
    refiner = TenantBriefRefiner(client=client)
    out = await refiner._call_haiku({"by_outcome_type": {}, "wins": 0, "losses": 0,
                                      "won_snippets": [], "lost_snippets": [],
                                      "customer_events": []})
    assert out == _empty_playbook()
