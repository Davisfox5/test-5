"""Unit tests for feedback diff helpers + WER + LLM-judge composite scoring."""

from __future__ import annotations

import pytest

from backend.app.services import feedback_service


def test_diff_summary_identical() -> None:
    summary = feedback_service.diff_summary("hello", "hello")
    assert summary["similarity"] == 1.0
    assert summary["edit_distance_normalized"] == 0.0
    assert summary["original_len"] == 5
    assert summary["updated_len"] == 5


def test_diff_summary_complete_change() -> None:
    summary = feedback_service.diff_summary("hello", "world")
    assert summary["similarity"] < 0.5
    assert summary["edit_distance_normalized"] > 0.5


def test_classify_reply_change_unchanged() -> None:
    event_type, payload = feedback_service.classify_reply_change(
        "Thanks, will follow up tomorrow.",
        "Thanks, will follow up tomorrow.",
    )
    assert event_type == "reply_sent_unchanged"
    assert payload["edit_distance_normalized"] == 0.0


def test_classify_reply_change_small_edit() -> None:
    original = "Thanks for reaching out, I will check with the team and get back to you tomorrow afternoon."
    edited = "Thanks for reaching out — I'll check with the team and get back to you tomorrow afternoon."
    event_type, payload = feedback_service.classify_reply_change(original, edited)
    assert event_type == "reply_edited_before_send"
    assert payload["edit_size"] == "small"


def test_classify_reply_change_large_edit() -> None:
    original = "Thanks for reaching out — I will get back to you with pricing tomorrow."
    edited = (
        "Hi, I really appreciate you reaching out about this. "
        "Let me loop in our solutions architect so we can put together a "
        "proper proposal that covers all of your team's requirements end to end."
    )
    event_type, payload = feedback_service.classify_reply_change(original, edited)
    assert event_type == "reply_edited_before_send"
    assert payload["edit_size"] == "large"


def test_classify_reply_change_empty_original() -> None:
    event_type, payload = feedback_service.classify_reply_change("", "anything goes")
    # Defensive: when there's no draft cached we don't fabricate an edit signal.
    assert event_type == "reply_sent_unchanged"


def test_diff_summary_handles_none_inputs() -> None:
    summary = feedback_service.diff_summary(None, None)  # type: ignore[arg-type]
    assert summary["original_len"] == 0
    assert summary["updated_len"] == 0
