"""Tests for the LINDA context-builder service."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services.kb.context_builder import (
    ContextBuilderService,
    _empty_brief,
    _validate_brief,
    format_brief_for_prompt,
)


def test_format_brief_empty_returns_empty_string():
    assert format_brief_for_prompt({}) == ""
    assert format_brief_for_prompt(_empty_brief()) == ""


def test_format_brief_renders_all_sections():
    brief = {
        "tenant_overview": "Acme sells widgets to SMBs.",
        "products_services": ["Pro plan", "Enterprise plan"],
        "pricing_summary": "Pro is $99/mo, Enterprise custom.",
        "policies": ["30-day refund", "Annual contracts"],
        "tone_and_voice": "Speak warmly but concisely.",
        "key_differentiators": ["24/7 support"],
        "known_objections": [
            {"objection": "Too expensive", "response": "Show ROI in 6 months."}
        ],
    }
    out = format_brief_for_prompt(brief)
    assert "# Tenant context" in out
    assert "Acme sells widgets" in out
    assert "Pro plan" in out
    assert "$99/mo" in out
    assert "30-day refund" in out
    assert "Speak warmly" in out
    assert "Too expensive" in out
    assert "Show ROI" in out


def test_format_brief_skips_empty_sections():
    brief = _empty_brief()
    brief["tenant_overview"] = "Just an overview."
    out = format_brief_for_prompt(brief)
    assert "Just an overview." in out
    assert "Products" not in out  # no products set, section hidden


def test_validate_brief_fills_missing_keys():
    partial = {"tenant_overview": "Hello"}
    out = _validate_brief(partial)
    assert out["tenant_overview"] == "Hello"
    assert out["products_services"] == []
    assert out["tone_and_voice"] == ""
    assert out["known_objections"] == []


def test_validate_brief_tolerates_wrong_types():
    bad = {"products_services": "not a list", "pricing_summary": 123}
    out = _validate_brief(bad)
    assert out["products_services"] == []
    assert out["pricing_summary"] == "123"


def test_validate_brief_non_dict_returns_empty():
    assert _validate_brief("oops") == _empty_brief()
    assert _validate_brief(None) == _empty_brief()


@pytest.mark.asyncio
async def test_call_haiku_merges_and_returns_validated_brief():
    """The Haiku call is mocked; we verify we pass the existing brief + new
    docs in and that the returned JSON is parsed and validated."""
    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                text=json.dumps(
                    {
                        "tenant_overview": "Acme sells widgets.",
                        "products_services": ["Pro plan"],
                        "pricing_summary": "Pro is $99/mo.",
                        "policies": [],
                        "tone_and_voice": "",
                        "key_differentiators": [],
                        "known_objections": [],
                    }
                )
            )
        ]
    )
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake_response)
    builder = ContextBuilderService(client=client)

    fake_doc = SimpleNamespace(
        id="doc1",
        title="Pricing FAQ",
        content="Pro plan costs $99 per month.",
    )
    out = await builder._call_haiku(_empty_brief(), [fake_doc])

    assert out["tenant_overview"] == "Acme sells widgets."
    assert out["products_services"] == ["Pro plan"]
    # Validate that the Haiku call was invoked with our prompt structure.
    call_kwargs = client.messages.create.await_args.kwargs
    assert "Pricing FAQ" in call_kwargs["messages"][0]["content"]
    assert "Existing brief" in call_kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_call_haiku_returns_existing_on_bad_json():
    """If the model returns malformed JSON, we preserve the existing brief
    rather than wiping it."""
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(text="this is not json")]
    )
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake_response)
    builder = ContextBuilderService(client=client)

    existing = _empty_brief()
    existing["tenant_overview"] = "Already known overview."
    fake_doc = SimpleNamespace(id="d", title="T", content="body")

    out = await builder._call_haiku(existing, [fake_doc])
    assert out == existing


def test_empty_brief_has_all_three_section_kinds():
    """Schema sanity: onboarding + learned sections exist in the default."""
    brief = _empty_brief()
    for key in ("goals", "kpis", "strategies", "org_structure", "personal_touches"):
        assert key in brief
    assert "playbook_insights" in brief
    assert isinstance(brief["personal_touches"], dict)
    assert isinstance(brief["playbook_insights"], dict)


def test_format_brief_renders_onboarding_and_playbook():
    brief = _empty_brief()
    brief["goals"] = ["10% MoM growth"]
    brief["kpis"] = [{"name": "CSAT", "target": 4.5, "current": 4.2}]
    brief["strategies"] = ["Lead with ROI for enterprise"]
    brief["personal_touches"] = {
        "greeting_style": "First name + Happy Monday",
        "signoff_style": "Warmly,",
        "phrasing_preferences": [{"say": "platform", "dont_say": "tool"}],
        "rituals": ["Handwritten note on closed-won >$10k"],
        "humor_level": "warm",
        "pacing_style": "match_caller",
        "empathy_markers": [],
        "celebration_markers": [],
        "avoid_phrases": ["guaranteed"],
        "signature_tagline": "",
    }
    brief["playbook_insights"] = {
        "what_works": ["Open with ROI"],
        "what_doesnt": ["Long demos"],
        "top_performing_phrases": [],
        "common_failure_modes": [],
        "winning_objection_handlers": [
            {"objection": "Too expensive", "handler": "6-month payback table"}
        ],
        "last_learned_at": "2026-04-19T00:00:00Z",
        "sample_size": 12,
    }
    out = format_brief_for_prompt(brief)
    assert "10% MoM growth" in out
    assert "CSAT" in out and "target 4.5" in out
    assert "Lead with ROI for enterprise" in out
    assert "First name + Happy Monday" in out
    assert "Warmly," in out
    assert "platform" in out and "tool" in out
    assert "Handwritten note" in out
    assert "guaranteed" in out
    assert "What's working recently" in out
    assert "Too expensive" in out
    assert "6-month payback table" in out


@pytest.mark.asyncio
async def test_call_haiku_preserves_onboarding_and_learned_sections():
    """Haiku rewrites only KB-derived sections; onboarding + learned are kept."""
    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                text=json.dumps(
                    {
                        "tenant_overview": "Updated overview from KB.",
                        # Hostile: Haiku tries to overwrite onboarding/learned
                        "goals": ["WRONG — should be ignored"],
                        "playbook_insights": {"what_works": ["WRONG"]},
                    }
                )
            )
        ]
    )
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=fake_response)
    builder = ContextBuilderService(client=client)

    existing = _empty_brief()
    existing["goals"] = ["Original goal from onboarding"]
    existing["playbook_insights"]["what_works"] = ["Original learned insight"]
    fake_doc = SimpleNamespace(id="d", title="T", content="body")

    out = await builder._call_haiku(existing, [fake_doc])
    # KB section got updated
    assert out["tenant_overview"] == "Updated overview from KB."
    # Onboarding + learned sections unchanged
    assert out["goals"] == ["Original goal from onboarding"]
    assert out["playbook_insights"]["what_works"] == ["Original learned insight"]
