"""Tests for the hardened POST /outcomes surface.

Covers HMAC verification, the Literal-typed outcome enum, and the
event-id fingerprinting helper.  Integration tests that exercise the
database-backed idempotency path live in test_integration once we have
a test DB fixture.
"""

import hashlib
import hmac
import uuid

import pytest

from backend.app.api.outcomes import (
    OutcomeEvent,
    OutcomeType,
    _autogen_event_id,
    _verify_hmac,
)


def test_outcome_type_enum_covers_core_calibrator_events():
    variants = set(OutcomeType.__args__)
    assert {
        "customer_replied",
        "customer_no_reply_72h",
        "contact_churned_30d",
        "contact_active_30d",
        "tenant_renewed",
        "tenant_upgraded",
        "deal_won",
        "deal_lost",
    }.issubset(variants)


def test_verify_hmac_passes_through_when_no_secret():
    assert _verify_hmac(None, b"body", None) is True
    assert _verify_hmac("", b"body", None) is True


def test_verify_hmac_accepts_valid_signature_with_or_without_prefix():
    secret = "s3cret"
    body = b"payload"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert _verify_hmac(secret, body, digest) is True
    assert _verify_hmac(secret, body, f"sha256={digest}") is True
    assert _verify_hmac(secret, body, f"  sha256={digest}  ") is True


def test_verify_hmac_rejects_missing_or_wrong_signature():
    assert _verify_hmac("s3cret", b"payload", None) is False
    assert _verify_hmac("s3cret", b"payload", "sha256=deadbeef") is False
    assert _verify_hmac("s3cret", b"payload", "") is False


def test_verify_hmac_is_sensitive_to_body_mutation():
    secret = "s3cret"
    digest = hmac.new(secret.encode(), b"original", hashlib.sha256).hexdigest()
    assert _verify_hmac(secret, b"original", digest) is True
    assert _verify_hmac(secret, b"tampered", digest) is False


def test_autogen_event_id_is_stable_for_same_input():
    ev = OutcomeEvent(
        interaction_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        outcome_type="customer_replied",
        occurred_at=None,
    )
    assert _autogen_event_id(ev) == _autogen_event_id(ev)


def test_autogen_event_id_differs_across_interactions():
    base = {
        "outcome_type": "customer_replied",
        "occurred_at": None,
    }
    a = OutcomeEvent(
        interaction_id=uuid.UUID("11111111-1111-1111-1111-111111111111"), **base
    )
    b = OutcomeEvent(
        interaction_id=uuid.UUID("22222222-2222-2222-2222-222222222222"), **base
    )
    assert _autogen_event_id(a) != _autogen_event_id(b)


def test_autogen_event_id_prefix_marks_inferred_origin():
    ev = OutcomeEvent(
        interaction_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        outcome_type="deal_won",
    )
    assert _autogen_event_id(ev).startswith("auto:")
