"""Tests for the Stripe billing integration.

Covers:
* ``verify_stripe_signature`` — valid roundtrip, tolerance window,
  tampered body, multiple ``v1`` values, malformed header, wrong secret.
* ``price_id_to_tier`` — resolves configured prices, ignores unconfigured.
* End-to-end event routing in ``_handle_event`` with a fake DB so the
  price→tier→apply_tier pipe is exercised without Postgres.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app.services import stripe_billing
from backend.app.services.stripe_billing import (
    price_id_to_tier,
    verify_stripe_signature,
)


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


# ── verify_stripe_signature ──────────────────────────────────────────


def test_signature_valid_roundtrip():
    secret = "whsec_test_1234"
    body = b'{"type":"customer.subscription.created"}'
    ts = int(time.time())
    sig = _sign(secret, ts, body)
    header = f"t={ts},v1={sig}"
    assert verify_stripe_signature(
        payload_bytes=body, signature_header=header, secret=secret
    )


def test_signature_rejects_tampered_body():
    secret = "whsec_x"
    body = b'{"type":"a"}'
    ts = int(time.time())
    sig = _sign(secret, ts, body)
    header = f"t={ts},v1={sig}"
    assert not verify_stripe_signature(
        payload_bytes=b'{"type":"b"}',
        signature_header=header,
        secret=secret,
    )


def test_signature_rejects_wrong_secret():
    secret = "whsec_correct"
    body = b"payload"
    ts = int(time.time())
    sig = _sign(secret, ts, body)
    header = f"t={ts},v1={sig}"
    assert not verify_stripe_signature(
        payload_bytes=body,
        signature_header=header,
        secret="whsec_wrong",
    )


def test_signature_rejects_stale_timestamp():
    secret = "whsec_x"
    body = b"payload"
    ts = int(time.time()) - 3600  # an hour ago
    sig = _sign(secret, ts, body)
    header = f"t={ts},v1={sig}"
    assert not verify_stripe_signature(
        payload_bytes=body, signature_header=header, secret=secret
    )


def test_signature_accepts_during_rotation_with_second_v1():
    """Two v1 values in the header means a rotation is in progress.
    Accept if any matches."""
    secret = "whsec_new"
    body = b"payload"
    ts = int(time.time())
    good = _sign(secret, ts, body)
    header = f"t={ts},v1=0000deadbeef,v1={good}"
    assert verify_stripe_signature(
        payload_bytes=body, signature_header=header, secret=secret
    )


def test_signature_rejects_malformed_header():
    for bad in ("", "nothing_useful", "v1=abc", "t=abc,v1=abc"):
        assert not verify_stripe_signature(
            payload_bytes=b"p",
            signature_header=bad,
            secret="whsec_x",
        )


def test_signature_tolerance_is_configurable():
    secret = "whsec_x"
    body = b"payload"
    ts = int(time.time()) - 120  # 2 minutes ago
    sig = _sign(secret, ts, body)
    header = f"t={ts},v1={sig}"
    # Default tolerance (300s) accepts 2 minutes.
    assert verify_stripe_signature(
        payload_bytes=body, signature_header=header, secret=secret
    )
    # Shrink tolerance below the age → reject.
    assert not verify_stripe_signature(
        payload_bytes=body,
        signature_header=header,
        secret=secret,
        tolerance_seconds=30,
    )


# ── price_id_to_tier ─────────────────────────────────────────────────


@pytest.fixture
def stripe_prices_env(monkeypatch):
    """Patch the Settings to return canned Stripe price ids."""
    def _fake_settings():
        return SimpleNamespace(
            STRIPE_PRICE_SOLO="price_solo",
            STRIPE_PRICE_TEAM="price_team",
            STRIPE_PRICE_PRO="price_pro",
            STRIPE_PRICE_ENTERPRISE="price_ent",
        )

    monkeypatch.setattr(stripe_billing, "get_settings", _fake_settings)
    return _fake_settings


def test_price_id_to_tier_resolves_known(stripe_prices_env):
    assert price_id_to_tier("price_solo") == "solo"
    assert price_id_to_tier("price_team") == "team"
    assert price_id_to_tier("price_pro") == "pro"
    assert price_id_to_tier("price_ent") == "enterprise"


def test_price_id_to_tier_unknown_returns_none(stripe_prices_env):
    assert price_id_to_tier("price_mystery") is None


def test_price_id_to_tier_empty_returns_none(stripe_prices_env):
    assert price_id_to_tier("") is None
    assert price_id_to_tier(None) is None  # type: ignore[arg-type]


def test_price_id_to_tier_never_matches_blank_entry(monkeypatch):
    """Unconfigured tiers have empty-string env values. Passing an empty
    string must never match — that would downgrade every unknown event."""
    def _fake_settings():
        return SimpleNamespace(
            STRIPE_PRICE_SOLO="",  # unconfigured
            STRIPE_PRICE_TEAM="price_team",
            STRIPE_PRICE_PRO="",
            STRIPE_PRICE_ENTERPRISE="",
        )

    monkeypatch.setattr(stripe_billing, "get_settings", _fake_settings)
    assert price_id_to_tier("") is None


# ── End-to-end event routing ─────────────────────────────────────────


class FakeTenant:
    def __init__(self, stripe_customer_id: str = ""):
        self.id = "tenant-uuid"
        self.subscription_tier = "solo"
        self.seat_limit = 1
        self.admin_seat_limit = 1
        self.features_enabled = {}
        self.stripe_customer_id = stripe_customer_id
        self.stripe_subscription_id = None


class FakeDB:
    """Minimal async DB double that answers the two query shapes our
    Stripe webhook + seat-reconciliation code path issue:

    1. ``SELECT tenant WHERE stripe_customer_id = …`` → one match.
    2. ``SELECT user WHERE tenant_id = … [AND is_active]`` → empty list.
    3. ``SELECT count(*) …`` → 0.

    Everything else returns an empty result so calls don't crash.
    """

    def __init__(self, tenants):
        self.tenants = tenants

    async def execute(self, stmt):
        try:
            params = stmt.compile().params
        except Exception:
            params = {}

        # Tenant-by-customer lookup.
        for v in params.values():
            if isinstance(v, str) and v.startswith("cus_"):
                match = next(
                    (t for t in self.tenants if t.stripe_customer_id == v),
                    None,
                )
                return _FakeResult(single=match, list_=[], count=0)

        # All other queries (user listings, counts) resolve to empties —
        # in these tests nobody has users seeded, so reconcile_seats has
        # nothing to suspend.
        return _FakeResult(single=None, list_=[], count=0)


class _FakeResult:
    def __init__(self, single=None, list_=None, count=0):
        self._single = single
        self._list = list(list_ or [])
        self._count = count

    def scalar_one_or_none(self):
        return self._single

    def scalar_one(self):
        return self._count

    def scalars(self):
        rows = self._list

        class _S:
            def all(self_inner):
                return rows

        return _S()


@pytest.mark.asyncio
async def test_subscription_created_applies_tier(stripe_prices_env):
    from backend.app.api.stripe_webhook import _handle_event

    tenant = FakeTenant(stripe_customer_id="cus_abc")
    db = FakeDB([tenant])

    obj = {
        "id": "sub_123",
        "customer": "cus_abc",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_pro"}}]},
    }
    result = await _handle_event(db, "customer.subscription.created", obj)
    assert result["handled"] is True
    assert result["tier"] == "pro"
    # apply_tier must have flipped the seat limits too.
    assert tenant.subscription_tier == "pro"
    assert tenant.seat_limit == 50
    assert tenant.admin_seat_limit == 3
    assert tenant.stripe_subscription_id == "sub_123"


@pytest.mark.asyncio
async def test_subscription_updated_handles_downgrade(stripe_prices_env):
    from backend.app.api.stripe_webhook import _handle_event

    tenant = FakeTenant(stripe_customer_id="cus_abc")
    tenant.subscription_tier = "pro"
    tenant.seat_limit = 50
    tenant.admin_seat_limit = 3
    db = FakeDB([tenant])

    obj = {
        "id": "sub_123",
        "customer": "cus_abc",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_team"}}]},
    }
    result = await _handle_event(db, "customer.subscription.updated", obj)
    assert result["tier"] == "team"
    # Seat limit shrinks, but we don't deactivate existing users — that's
    # the non-retroactive policy. Just the cap changes.
    assert tenant.seat_limit == 10


@pytest.mark.asyncio
async def test_subscription_deleted_drops_to_solo(stripe_prices_env):
    from backend.app.api.stripe_webhook import _handle_event

    tenant = FakeTenant(stripe_customer_id="cus_abc")
    tenant.subscription_tier = "enterprise"
    tenant.seat_limit = 500
    tenant.admin_seat_limit = 20
    tenant.stripe_subscription_id = "sub_old"
    db = FakeDB([tenant])

    obj = {"id": "sub_old", "customer": "cus_abc", "status": "canceled"}
    result = await _handle_event(db, "customer.subscription.deleted", obj)
    assert result["handled"] is True
    assert result["tier"] == "solo"
    assert tenant.subscription_tier == "solo"
    assert tenant.seat_limit == 1
    assert tenant.stripe_subscription_id is None


@pytest.mark.asyncio
async def test_unknown_customer_is_ignored_not_crashed(stripe_prices_env):
    from backend.app.api.stripe_webhook import _handle_event

    db = FakeDB([])  # no tenants
    obj = {
        "id": "sub_123",
        "customer": "cus_mystery",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_pro"}}]},
    }
    result = await _handle_event(db, "customer.subscription.created", obj)
    assert result["handled"] is False
    assert result["reason"] == "unknown_customer"


@pytest.mark.asyncio
async def test_unknown_price_does_not_change_tier(stripe_prices_env):
    from backend.app.api.stripe_webhook import _handle_event

    tenant = FakeTenant(stripe_customer_id="cus_abc")
    tenant.subscription_tier = "team"
    tenant.seat_limit = 10
    db = FakeDB([tenant])

    obj = {
        "id": "sub_123",
        "customer": "cus_abc",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_unknown"}}]},
    }
    result = await _handle_event(db, "customer.subscription.updated", obj)
    assert result["handled"] is False
    assert result["reason"] == "unknown_price"
    # Tier untouched.
    assert tenant.subscription_tier == "team"
    assert tenant.seat_limit == 10


@pytest.mark.asyncio
async def test_event_type_not_recognised_is_200(stripe_prices_env):
    from backend.app.api.stripe_webhook import _handle_event

    result = await _handle_event(FakeDB([]), "invoice.paid", {})
    assert result == {"handled": False, "event_type": "invoice.paid"}
