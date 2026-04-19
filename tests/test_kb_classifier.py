"""Tests for the question classifier — focusing on the cheap paths."""

import pytest

from backend.app.services.kb.classifier import classify


@pytest.mark.asyncio
async def test_deepgram_keyterm_hit_always_fires():
    result = await classify(
        "hey man just checking in",
        deepgram_keyterm_hit=True,
    )
    assert result.is_question is True
    assert result.source == "deepgram_keyterm"


@pytest.mark.asyncio
async def test_question_mark_fires_regex_path():
    result = await classify("How much does the pro plan cost?")
    assert result.is_question is True
    assert result.source == "regex"
    assert result.urgency == "high"


@pytest.mark.asyncio
async def test_statement_without_objection_is_skipped():
    # No haiku fallback so this is deterministic.
    result = await classify(
        "Thanks, I appreciate the demo today.",
        use_haiku_fallback=False,
    )
    assert result.is_question is False
    assert result.source == "skipped"


@pytest.mark.asyncio
async def test_empty_text_returns_skipped():
    result = await classify("   ")
    assert result.is_question is False
    assert result.source == "skipped"
