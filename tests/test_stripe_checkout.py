"""Tests for ``POST /admin/stripe/checkout``.

We mock ``_stripe_post`` so the endpoint never reaches Stripe; instead
we capture the form payload it would have sent and assert on the
``line_items`` + ``add_invoice_items`` shape.

Patterns mirror ``tests/test_backend_gap_fixes.py`` (the existing
billing-portal tests) for consistency with the rest of the suite.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.api import stripe_webhook


# ── Catalog / settings helpers ───────────────────────────────────────


def _full_catalog() -> Dict[str, Any]:
    """Minimal-but-complete price catalog the endpoint resolves against.

    Each tier has every leaf populated so the happy path resolves. Tests
    can json.dumps a stripped-down copy if they want to test missing
    entries.
    """
    tier_block = {
        "base": {"monthly": "price_TIER_base_m", "annual": "price_TIER_base_a"},
        "addl_seat": {"monthly": "price_TIER_seat_m", "annual": "price_TIER_seat_a"},
        "extra_scorecard": {
            "monthly": "price_TIER_sc_m",
            "annual": "price_TIER_sc_a",
        },
        "onboarding": {"direct": "price_TIER_onb_direct", "partner": "price_TIER_onb_partner"},
    }

    def _for(tier: str) -> Dict[str, Any]:
        return json.loads(json.dumps(tier_block).replace("TIER", tier))

    return {
        "starter": _for("starter"),
        "growth": _for("growth"),
        "enterprise": _for("enterprise"),
        "starter_addons": {
            "live_coaching": {"monthly": "price_starter_coach_m", "annual": "price_starter_coach_a"},
        },
    }


def _settings(api_key: str = "sk_test_xyz", catalog=None) -> SimpleNamespace:
    if catalog is None:
        catalog = _full_catalog()
    raw = json.dumps(catalog) if isinstance(catalog, dict) else (catalog or "")
    return SimpleNamespace(STRIPE_API_KEY=api_key, STRIPE_PRICE_CATALOG=raw)


def _principal(stripe_subscription_id=None, stripe_customer_id="cus_existing"):
    tenant = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        slug="acme",
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
    )
    user = SimpleNamespace(email="admin@example.com")
    return SimpleNamespace(tenant=tenant, user=user)


def _capture_post(monkeypatch, *, session=None):
    """Patch ``_stripe_post`` to capture (path, form) calls and return
    a canned session JSON. Returns the calls list for assertion."""
    captured: List[tuple[str, Dict[str, str]]] = []
    if session is None:
        session = {
            "id": "cs_test_1",
            "url": "https://checkout.stripe.com/c/pay/cs_test_1",
            "expires_at": 1234567890,
        }

    async def fake_post(api_key, path, form):
        captured.append((path, dict(form)))
        if path == "/customers":
            return {"id": "cus_new123"}
        if path == "/checkout/sessions":
            return session
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(stripe_webhook, "_stripe_post", fake_post)
    return captured


# ── Pydantic validation ──────────────────────────────────────────────


def test_checkout_request_rejects_negative_addl_seats():
    with pytest.raises(ValidationError):
        stripe_webhook.CheckoutRequest(
            tier="starter",
            addl_seats=-1,
            success_url="https://x/y",
            cancel_url="https://x/z",
        )


def test_checkout_request_rejects_negative_extra_scorecards():
    with pytest.raises(ValidationError):
        stripe_webhook.CheckoutRequest(
            tier="starter",
            extra_scorecards=-1,
            success_url="https://x/y",
            cancel_url="https://x/z",
        )


def test_checkout_request_rejects_unknown_tier():
    with pytest.raises(ValidationError):
        stripe_webhook.CheckoutRequest(
            tier="solo",  # type: ignore[arg-type]
            success_url="https://x/y",
            cancel_url="https://x/z",
        )


# ── Pre-flight gates ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkout_503_when_stripe_api_key_missing(monkeypatch):
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: _settings(api_key=""))
    body = stripe_webhook.CheckoutRequest(
        tier="starter", success_url="https://x/s", cancel_url="https://x/c"
    )
    with pytest.raises(HTTPException) as exc_info:
        await stripe_webhook.create_stripe_checkout_session(
            body=body, principal=_principal(), db=SimpleNamespace()
        )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_checkout_503_when_catalog_missing(monkeypatch):
    monkeypatch.setattr(
        stripe_webhook,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_API_KEY="sk_test_xyz", STRIPE_PRICE_CATALOG=""),
    )
    body = stripe_webhook.CheckoutRequest(
        tier="starter", success_url="https://x/s", cancel_url="https://x/c"
    )
    with pytest.raises(HTTPException) as exc_info:
        await stripe_webhook.create_stripe_checkout_session(
            body=body, principal=_principal(), db=SimpleNamespace()
        )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_checkout_409_when_already_subscribed(monkeypatch):
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: _settings())
    body = stripe_webhook.CheckoutRequest(
        tier="starter", success_url="https://x/s", cancel_url="https://x/c"
    )
    with pytest.raises(HTTPException) as exc_info:
        await stripe_webhook.create_stripe_checkout_session(
            body=body,
            principal=_principal(stripe_subscription_id="sub_existing"),
            db=SimpleNamespace(),
        )
    assert exc_info.value.status_code == 409


# ── Happy paths: assert line_items + add_invoice_items shape ────────


def _line_items(form: Dict[str, str]) -> List[Dict[str, str]]:
    """Reconstruct the ``line_items`` list from the indexed form keys."""
    items: Dict[int, Dict[str, str]] = {}
    for k, v in form.items():
        if not k.startswith("line_items["):
            continue
        # k like "line_items[0][price]"
        idx_part, _, rest = k[len("line_items[") :].partition("]")
        field = rest.split("[")[1].rstrip("]")
        items.setdefault(int(idx_part), {})[field] = v
    return [items[i] for i in sorted(items)]


def _invoice_items(form: Dict[str, str]) -> List[Dict[str, str]]:
    items: Dict[int, Dict[str, str]] = {}
    prefix = "add_invoice_items["
    for k, v in form.items():
        if not k.startswith(prefix):
            continue
        idx_part, _, rest = k[len(prefix) :].partition("]")
        field = rest.split("[")[1].rstrip("]")
        items.setdefault(int(idx_part), {})[field] = v
    return [items[i] for i in sorted(items)]


@pytest.mark.asyncio
async def test_checkout_plain_starter_monthly_direct(monkeypatch):
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: _settings())
    captured = _capture_post(monkeypatch)

    body = stripe_webhook.CheckoutRequest(
        tier="starter",
        cycle="monthly",
        success_url="https://x/s",
        cancel_url="https://x/c",
    )
    out = await stripe_webhook.create_stripe_checkout_session(
        body=body, principal=_principal(), db=SimpleNamespace()
    )
    assert out["url"].startswith("https://checkout.stripe.com/")

    # Only one Stripe call (existing customer → no /customers POST).
    assert [p for p, _ in captured] == ["/checkout/sessions"]
    _, form = captured[0]

    lines = _line_items(form)
    assert len(lines) == 1
    assert lines[0] == {"price": "price_starter_base_m", "quantity": "1"}

    invoice_items = _invoice_items(form)
    assert invoice_items == [{"price": "price_starter_onb_direct", "quantity": "1"}]


@pytest.mark.asyncio
async def test_checkout_growth_annual_partner_with_seats_and_scorecards(monkeypatch):
    """Growth annual + partner onboarding + 5 add'l seats + 2 extra
    scorecards → 3 line_items (base, addl_seat × 5, extra_sc × 2) + 1
    partner onboarding invoice item."""
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: _settings())
    captured = _capture_post(monkeypatch)

    body = stripe_webhook.CheckoutRequest(
        tier="growth",
        cycle="annual",
        is_partner=True,
        addl_seats=5,
        extra_scorecards=2,
        success_url="https://x/s",
        cancel_url="https://x/c",
    )
    await stripe_webhook.create_stripe_checkout_session(
        body=body, principal=_principal(), db=SimpleNamespace()
    )

    _, form = captured[-1]
    lines = _line_items(form)
    assert lines == [
        {"price": "price_growth_base_a", "quantity": "1"},
        {"price": "price_growth_seat_a", "quantity": "5"},
        {"price": "price_growth_sc_a", "quantity": "2"},
    ]

    invoice_items = _invoice_items(form)
    # Partner audience → partner onboarding price.
    assert invoice_items == [{"price": "price_growth_onb_partner", "quantity": "1"}]


@pytest.mark.asyncio
async def test_checkout_starter_with_live_coaching_seats(monkeypatch):
    """Starter monthly with 3 live-coaching seats → 2 line items (base
    + coaching) + 1 onboarding invoice item."""
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: _settings())
    captured = _capture_post(monkeypatch)

    body = stripe_webhook.CheckoutRequest(
        tier="starter",
        cycle="monthly",
        live_coaching_seats=3,
        success_url="https://x/s",
        cancel_url="https://x/c",
    )
    await stripe_webhook.create_stripe_checkout_session(
        body=body, principal=_principal(), db=SimpleNamespace()
    )

    _, form = captured[-1]
    lines = _line_items(form)
    assert lines == [
        {"price": "price_starter_base_m", "quantity": "1"},
        {"price": "price_starter_coach_m", "quantity": "3"},
    ]


@pytest.mark.asyncio
async def test_checkout_growth_silently_drops_live_coaching(monkeypatch):
    """Live-coaching is bundled into Growth + Enterprise. Specifying
    ``live_coaching_seats > 0`` on those tiers must silently drop the
    line rather than 400 — the SPA might pass it from a shared form."""
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: _settings())
    captured = _capture_post(monkeypatch)

    body = stripe_webhook.CheckoutRequest(
        tier="growth",
        cycle="monthly",
        live_coaching_seats=3,
        success_url="https://x/s",
        cancel_url="https://x/c",
    )
    await stripe_webhook.create_stripe_checkout_session(
        body=body, principal=_principal(), db=SimpleNamespace()
    )

    _, form = captured[-1]
    lines = _line_items(form)
    assert lines == [{"price": "price_growth_base_m", "quantity": "1"}]


@pytest.mark.asyncio
async def test_checkout_mints_customer_when_tenant_has_none(monkeypatch):
    """First-time subscriber path: tenant has no stripe_customer_id →
    we POST /customers first, persist the id back, then POST /checkout/sessions."""
    monkeypatch.setattr(stripe_webhook, "get_settings", lambda: _settings())
    captured = _capture_post(monkeypatch)

    principal = _principal(stripe_customer_id=None)
    body = stripe_webhook.CheckoutRequest(
        tier="starter",
        success_url="https://x/s",
        cancel_url="https://x/c",
    )
    await stripe_webhook.create_stripe_checkout_session(
        body=body, principal=principal, db=SimpleNamespace()
    )

    paths = [p for p, _ in captured]
    assert paths == ["/customers", "/checkout/sessions"]
    # Newly minted id must be persisted on the tenant.
    assert principal.tenant.stripe_customer_id == "cus_new123"
    # And used as the checkout customer.
    _, checkout_form = captured[-1]
    assert checkout_form["customer"] == "cus_new123"


@pytest.mark.asyncio
async def test_checkout_503_on_partial_catalog(monkeypatch):
    """Catalog parses but is missing the partner onboarding leaf for
    the requested tier → 503 with a descriptive message."""
    catalog = _full_catalog()
    catalog["starter"]["onboarding"].pop("partner")
    monkeypatch.setattr(
        stripe_webhook, "get_settings", lambda: _settings(catalog=catalog)
    )
    body = stripe_webhook.CheckoutRequest(
        tier="starter",
        is_partner=True,
        success_url="https://x/s",
        cancel_url="https://x/c",
    )
    with pytest.raises(HTTPException) as exc_info:
        await stripe_webhook.create_stripe_checkout_session(
            body=body, principal=_principal(), db=SimpleNamespace()
        )
    assert exc_info.value.status_code == 503
    assert "starter.onboarding.partner" in str(exc_info.value.detail)
