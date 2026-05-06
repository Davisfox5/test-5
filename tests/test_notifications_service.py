"""Tests for the notifications service vocabulary and helpers."""

from __future__ import annotations

from backend.app.services.notifications import (
    NotificationKind,
    VALID_KINDS,
)


def test_notification_kinds_match_migration_check_constraint():
    # The Phase 5B-6 migration's CHECK constraint lists exactly these.
    expected = {
        "action_item_assigned",
        "action_item_comment",
        "action_item_returned",
        "action_item_due_soon",
        "action_item_overdue",
        "manager_review_completed",
        "scorecard_review_assigned",
        "system",
        "other",
    }
    assert VALID_KINDS == expected


def test_kind_constants_are_strings():
    for name in dir(NotificationKind):
        if name.startswith("_"):
            continue
        value = getattr(NotificationKind, name)
        assert isinstance(value, str), f"{name} is not a string"


def test_action_item_kinds_present():
    assert NotificationKind.ACTION_ITEM_ASSIGNED == "action_item_assigned"
    assert NotificationKind.ACTION_ITEM_COMMENT == "action_item_comment"
    assert NotificationKind.ACTION_ITEM_RETURNED == "action_item_returned"
