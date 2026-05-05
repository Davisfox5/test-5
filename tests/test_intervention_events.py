"""Tests for the intervention events service (Phase 0 telemetry)."""

from __future__ import annotations

from backend.app.services.intervention_events import (
    InterventionKind,
    VALID_KINDS,
    action_item_kind_for_transition,
)


# ── Kind vocabulary ─────────────────────────────────────────────────────


def test_intervention_kinds_match_migration_check_constraint():
    # The Phase 0 migration's CHECK constraint lists exactly these. If
    # this test fails, the migration and the service have drifted; one
    # side or the other needs updating.
    expected = {
        "follow_up_sent",
        "manager_review",
        "escalation",
        "rep_callback",
        "discount_offered",
        "action_item_completed",
        "action_item_dismissed",
        "action_item_snoozed",
        "action_item_reopened",
        "scorecard_review_completed",
        "other",
    }
    assert VALID_KINDS == expected


def test_kind_constants_are_strings():
    for name in dir(InterventionKind):
        if name.startswith("_"):
            continue
        value = getattr(InterventionKind, name)
        assert isinstance(value, str), f"{name} is not a string"


# ── action_item_kind_for_transition ─────────────────────────────────────


def test_transition_to_completed_returns_completed_kind():
    assert action_item_kind_for_transition("pending", "completed") == "action_item_completed"
    assert action_item_kind_for_transition("pending", "done") == "action_item_completed"


def test_transition_to_dismissed_returns_dismissed_kind():
    assert action_item_kind_for_transition("pending", "dismissed") == "action_item_dismissed"
    assert action_item_kind_for_transition("pending", "rejected") == "action_item_dismissed"


def test_transition_to_snoozed_returns_snoozed_kind():
    assert action_item_kind_for_transition("pending", "snoozed") == "action_item_snoozed"


def test_reopening_to_pending_returns_reopened_kind():
    assert action_item_kind_for_transition("completed", "pending") == "action_item_reopened"
    assert action_item_kind_for_transition("dismissed", "open") == "action_item_reopened"
    assert action_item_kind_for_transition("snoozed", "in_progress") == "action_item_reopened"


def test_no_change_returns_none():
    assert action_item_kind_for_transition("pending", "pending") is None
    assert action_item_kind_for_transition("Pending", "PENDING") is None  # case-insensitive


def test_unknown_new_status_returns_none():
    assert action_item_kind_for_transition("pending", "vibing") is None


def test_missing_new_status_returns_none():
    assert action_item_kind_for_transition("pending", None) is None
    assert action_item_kind_for_transition("pending", "") is None


def test_missing_old_status_still_records_terminal_transition():
    # First-ever transition (no prior status known) should still record
    # the new state as an intervention.
    assert action_item_kind_for_transition(None, "completed") == "action_item_completed"
