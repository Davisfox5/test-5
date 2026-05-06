"""Tests for the category taxonomy normalization helpers."""

from __future__ import annotations

from backend.app.services.category_taxonomy import _normalize


def test_normalize_lowercases():
    assert _normalize("Follow Up") == "follow_up"


def test_normalize_strips_whitespace():
    assert _normalize("  follow_up  ") == "follow_up"


def test_normalize_collapses_multiple_spaces():
    assert _normalize("follow   up") == "follow_up"


def test_normalize_replaces_hyphens_with_underscores():
    assert _normalize("follow-up") == "follow_up"


def test_normalize_handles_mixed_separators():
    assert _normalize("Compliance Remediation") == "compliance_remediation"
    assert _normalize("compliance-remediation") == "compliance_remediation"
    assert _normalize("compliance_remediation") == "compliance_remediation"
    # All three variants normalize to the same canonical form.


def test_normalize_preserves_already_canonical():
    assert _normalize("commitment_made") == "commitment_made"


def test_normalize_handles_single_word():
    assert _normalize("escalation") == "escalation"
    assert _normalize("ESCALATION") == "escalation"
