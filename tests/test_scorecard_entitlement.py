"""Tests for the scorecard entitlement helpers + the ``POST /scorecards``
402 cap behaviour.

Covers:
* Pure ``included_scorecards_for_seats`` math.
* ``count_paid_extra_scorecards`` reading line-item quantities from a
  Stripe-shaped subscription dict.
* ``compute_entitlement`` end-to-end — DB counts via a fake session
  plus a mocked Stripe ``GET /subscriptions/{id}``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from backend.app.services import scorecard_entitlement
from backend.app.services.scorecard_entitlement import (
    EntitlementInfo,
    compute_entitlement,
    count_paid_extra_scorecards,
    included_scorecards_for_seats,
    parse_price_catalog,
)


# ── included_scorecards_for_seats ────────────────────────────────────


@pytest.mark.parametrize(
    "seats,expected",
    [
        (0, 1),
        (1, 1),
        (5, 1),
        (10, 1),
        (11, 2),
        (20, 2),
        (25, 3),
        (50, 5),
        (101, 11),
    ],
)
def test_included_scorecards_for_seats(seats, expected):
    assert included_scorecards_for_seats(seats) == expected


def test_included_scorecards_for_seats_floor_at_one():
    """Even a tenant with no users yet gets a single scorecard so the
    onboarding flow doesn't trip the cap on first run."""
    assert included_scorecards_for_seats(0) == 1
    assert included_scorecards_for_seats(-3) == 1


# ── parse_price_catalog ──────────────────────────────────────────────


def test_parse_price_catalog_empty():
    assert parse_price_catalog("") == {}
    assert parse_price_catalog("   ") == {}


def test_parse_price_catalog_invalid_json_logs_and_returns_empty(caplog):
    out = parse_price_catalog("{not json")
    assert out == {}


def test_parse_price_catalog_non_object_returns_empty():
    assert parse_price_catalog("[1,2,3]") == {}
    assert parse_price_catalog('"a string"') == {}


def test_parse_price_catalog_round_trips():
    blob = {"starter": {"base": {"monthly": "price_x"}}}
    parsed = parse_price_catalog(json.dumps(blob))
    assert parsed == blob


# ── count_paid_extra_scorecards ──────────────────────────────────────


def _settings_with_catalog(**catalog_overrides) -> SimpleNamespace:
    catalog = {
        "starter": {
            "extra_scorecard": {
                "monthly": "price_starter_sc_m",
                "annual": "price_starter_sc_a",
            }
        },
        "growth": {
            "extra_scorecard": {
                "monthly": "price_growth_sc_m",
                "annual": "price_growth_sc_a",
            }
        },
        "enterprise": {
            "extra_scorecard": {
                "monthly": "price_ent_sc_m",
                "annual": "price_ent_sc_a",
            }
        },
    }
    catalog.update(catalog_overrides)
    return SimpleNamespace(STRIPE_PRICE_CATALOG=json.dumps(catalog))


def test_count_paid_extra_scorecards_empty_subscription():
    settings = _settings_with_catalog()
    assert count_paid_extra_scorecards({}, settings) == 0
    assert count_paid_extra_scorecards({"items": {}}, settings) == 0
    assert count_paid_extra_scorecards({"items": {"data": []}}, settings) == 0


def test_count_paid_extra_scorecards_single_line_two_quantity():
    settings = _settings_with_catalog()
    sub = {
        "items": {
            "data": [
                {"price": {"id": "price_starter_sc_m"}, "quantity": 2},
            ]
        }
    }
    assert count_paid_extra_scorecards(sub, settings) == 2


def test_count_paid_extra_scorecards_mixed_lines():
    """Tier base + scorecard add-on: only the scorecard line counts."""
    settings = _settings_with_catalog()
    sub = {
        "items": {
            "data": [
                {"price": {"id": "price_growth_base"}, "quantity": 1},
                {"price": {"id": "price_growth_sc_m"}, "quantity": 3},
                {"price": {"id": "price_growth_addl_seat"}, "quantity": 7},
            ]
        }
    }
    assert count_paid_extra_scorecards(sub, settings) == 3


def test_count_paid_extra_scorecards_counts_any_tier():
    """A tenant might have moved tiers mid-subscription; we sum across
    every tier's extra-scorecard SKU rather than gating by current tier."""
    settings = _settings_with_catalog()
    sub = {
        "items": {
            "data": [
                {"price": {"id": "price_starter_sc_m"}, "quantity": 1},
                {"price": {"id": "price_growth_sc_a"}, "quantity": 4},
            ]
        }
    }
    assert count_paid_extra_scorecards(sub, settings) == 5


def test_count_paid_extra_scorecards_no_catalog_returns_zero():
    settings = SimpleNamespace(STRIPE_PRICE_CATALOG="")
    sub = {"items": {"data": [{"price": {"id": "price_x"}, "quantity": 5}]}}
    assert count_paid_extra_scorecards(sub, settings) == 0


# ── compute_entitlement (async, with mocked DB + Stripe) ─────────────


class _FakeResult:
    def __init__(self, count: int):
        self._count = count

    def scalar_one(self):
        return self._count


class _FakeDB:
    """Minimal AsyncSession double that returns canned counts.

    The two queries ``compute_entitlement`` issues are both
    ``select(func.count()).select_from(...)`` — we differentiate by
    inspecting the FROM clause table name.
    """

    def __init__(self, *, active_users: int, scorecards: int):
        self._active_users = active_users
        self._scorecards = scorecards

    async def execute(self, stmt):
        sql = str(stmt).lower()
        if "users" in sql:
            return _FakeResult(self._active_users)
        if "scorecard_templates" in sql:
            return _FakeResult(self._scorecards)
        return _FakeResult(0)


class _FakeHttpResponse:
    def __init__(self, status_code: int, payload: Dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Drop-in for httpx.AsyncClient that yields a canned response and
    records the calls it received."""

    def __init__(self, response: _FakeHttpResponse):
        self._response = response
        self.calls: List[tuple[str, Dict[str, str]]] = []

    async def get(self, url: str, headers: Optional[Dict[str, str]] = None):
        self.calls.append((url, dict(headers or {})))
        return self._response


@pytest.mark.asyncio
async def test_compute_entitlement_includes_paid_extras_from_stripe():
    """compute_entitlement: 12 active users → 2 included; Stripe sub
    has 2 extra-scorecard line items × 2 each = 4 paid_extra; 3 templates
    already exist → used=3, total=6, room for three more."""
    settings = _settings_with_catalog()
    settings.STRIPE_API_KEY = "sk_test_xyz"

    sub_payload = {
        "id": "sub_abc",
        "items": {
            "data": [
                {"price": {"id": "price_growth_base"}, "quantity": 1},
                {"price": {"id": "price_starter_sc_m"}, "quantity": 2},
                {"price": {"id": "price_growth_sc_m"}, "quantity": 2},
            ]
        },
    }
    fake_client = _FakeHttpClient(_FakeHttpResponse(200, sub_payload))

    tenant = SimpleNamespace(
        id="tenant-uuid",
        stripe_subscription_id="sub_abc",
    )
    db = _FakeDB(active_users=12, scorecards=3)

    info = await compute_entitlement(
        db, tenant, settings=settings, http_client=fake_client
    )
    assert isinstance(info, EntitlementInfo)
    assert info.included == 2  # ceil(12/10)
    assert info.paid_extra == 4  # 2 + 2
    assert info.total == 6
    assert info.used == 3

    # Verify the Stripe URL was hit with the bearer token.
    assert len(fake_client.calls) == 1
    url, headers = fake_client.calls[0]
    assert url.endswith("/v1/subscriptions/sub_abc")
    assert headers["Authorization"] == "Bearer sk_test_xyz"


@pytest.mark.asyncio
async def test_compute_entitlement_skips_stripe_when_no_subscription():
    """A tenant without ``stripe_subscription_id`` (e.g. sandbox /
    self-hosted) gets only the included entitlement — no Stripe call."""
    settings = _settings_with_catalog()
    settings.STRIPE_API_KEY = "sk_test_xyz"

    tenant = SimpleNamespace(id="t", stripe_subscription_id=None)
    db = _FakeDB(active_users=25, scorecards=1)
    fake_client = _FakeHttpClient(_FakeHttpResponse(500, {}))

    info = await compute_entitlement(
        db, tenant, settings=settings, http_client=fake_client
    )
    assert info.included == 3  # ceil(25/10)
    assert info.paid_extra == 0
    assert info.total == 3
    assert info.used == 1
    assert fake_client.calls == []  # Stripe never touched


@pytest.mark.asyncio
async def test_compute_entitlement_handles_stripe_error_gracefully():
    """Stripe down → fall back to included-only. We must not block
    scorecard creation on Stripe availability when the cap isn't even
    in question (tenant well within included)."""
    settings = _settings_with_catalog()
    settings.STRIPE_API_KEY = "sk_test_xyz"

    tenant = SimpleNamespace(id="t", stripe_subscription_id="sub_abc")
    db = _FakeDB(active_users=10, scorecards=0)
    fake_client = _FakeHttpClient(_FakeHttpResponse(503, {"error": "down"}))

    info = await compute_entitlement(
        db, tenant, settings=settings, http_client=fake_client
    )
    assert info.included == 1
    assert info.paid_extra == 0
    assert info.total == 1
    assert info.used == 0


# ── Endpoint integration: 402 when at cap ────────────────────────────


@pytest.mark.asyncio
async def test_post_scorecards_returns_402_when_at_cap(monkeypatch):
    """The endpoint must surface a 402 with ``limit`` + ``current`` in
    the detail body so the SPA upgrade-prompt has the data it needs."""
    from fastapi import HTTPException

    from backend.app.api import scorecards as scorecards_api

    async def fake_compute(db, tenant):
        return EntitlementInfo(included=1, paid_extra=0, total=1, used=1)

    monkeypatch.setattr(scorecards_api, "compute_entitlement", fake_compute)

    body = scorecards_api.ScorecardTemplateCreate(name="x", criteria=[{"k": "v"}])
    tenant = SimpleNamespace(id="t")
    db = SimpleNamespace()  # never touched: we 402 before any write

    with pytest.raises(HTTPException) as exc_info:
        await scorecards_api.create_scorecard_template(body=body, db=db, tenant=tenant)

    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["limit"] == 1
    assert detail["current"] == 1
    assert detail["detail"] == "Scorecard cap reached"
