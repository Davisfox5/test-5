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
        "company_overview": "Acme sells widgets to SMBs.",
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
    assert "# Company context" in out
    assert "Acme sells widgets" in out
    assert "Pro plan" in out
    assert "$99/mo" in out
    assert "30-day refund" in out
    assert "Speak warmly" in out
    assert "Too expensive" in out
    assert "Show ROI" in out


def test_format_brief_skips_empty_sections():
    brief = _empty_brief()
    brief["company_overview"] = "Just an overview."
    out = format_brief_for_prompt(brief)
    assert "Just an overview." in out
    assert "Products" not in out  # no products set, section hidden


def test_validate_brief_fills_missing_keys():
    partial = {"company_overview": "Hello"}
    out = _validate_brief(partial)
    assert out["company_overview"] == "Hello"
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
                        "company_overview": "Acme sells widgets.",
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

    assert out["company_overview"] == "Acme sells widgets."
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
    existing["company_overview"] = "Already known overview."
    fake_doc = SimpleNamespace(id="d", title="T", content="body")

    out = await builder._call_haiku(existing, [fake_doc])
    assert out == existing
