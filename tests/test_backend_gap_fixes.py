"""Targeted coverage for the seven backend gaps closed by this PR.

Each test is the minimum needed to lock in the contract change; the
broader integration paths are exercised by the existing suite.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


# ── Gap 3: action-item status-filter aliasing ────────────────────────


def test_status_filter_open_maps_to_pending_and_in_progress():
    from backend.app.api.action_items import _expand_status_filter

    assert sorted(_expand_status_filter("open")) == ["in_progress", "open", "pending"]


def test_status_filter_done_maps_to_done_and_completed():
    from backend.app.api.action_items import _expand_status_filter

    assert sorted(_expand_status_filter("done")) == ["completed", "done"]
    assert sorted(_expand_status_filter("completed")) == ["completed", "done"]


def test_status_filter_canonical_singletons():
    from backend.app.api.action_items import _expand_status_filter

    assert _expand_status_filter("snoozed") == ["snoozed"]
    assert _expand_status_filter("pending") == ["pending"]
    assert _expand_status_filter("in_progress") == ["in_progress"]


def test_status_filter_unknown_value_passes_through():
    """Unknown statuses fall through verbatim so the SQL is still
    well-formed and produces an empty result, instead of 422-ing."""
    from backend.app.api.action_items import _expand_status_filter

    assert _expand_status_filter("totally_made_up") == ["totally_made_up"]


# ── Gap 4: ai-health resilience on empty / missing tables ────────────


@pytest.mark.asyncio
async def test_scalar_or_default_returns_default_when_table_missing():
    from backend.app.api.analytics import _scalar_or_default

    class FakeDB:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("relation 'wer_metrics' does not exist")

        async def rollback(self):
            return None

    out = await _scalar_or_default(FakeDB(), "SELECT 1", {}, default=0)
    assert out == 0


@pytest.mark.asyncio
async def test_scalar_or_default_returns_value_when_present():
    from backend.app.api.analytics import _scalar_or_default

    class _Row:
        def __getitem__(self, idx):
            return 7.5

    class _Result:
        def fetchone(self):
            return _Row()

    class FakeDB:
        async def execute(self, *args, **kwargs):
            return _Result()

    out = await _scalar_or_default(FakeDB(), "SELECT 1", {}, default=None)
    assert out == 7.5


@pytest.mark.asyncio
async def test_scalar_or_default_treats_null_row_as_default():
    from backend.app.api.analytics import _scalar_or_default

    class _Row:
        def __getitem__(self, idx):
            return None

    class _Result:
        def fetchone(self):
            return _Row()

    class FakeDB:
        async def execute(self, *args, **kwargs):
            return _Result()

    out = await _scalar_or_default(FakeDB(), "SELECT AVG(x)", {}, default=None)
    assert out is None


# ── Gap 6: stripe portal — config-missing returns 503 ────────────────


@pytest.mark.asyncio
async def test_stripe_portal_503_when_api_key_unset(monkeypatch):
    """The endpoint must surface a 503, not a crash, when STRIPE_API_KEY
    is unset on the deployment (staging today)."""
    from fastapi import HTTPException

    from backend.app.api import stripe_webhook
    from backend.app.api.stripe_webhook import open_stripe_billing_portal

    monkeypatch.setattr(
        stripe_webhook,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_API_KEY=""),
    )

    tenant = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        slug="acme",
        stripe_customer_id="",
    )
    principal = SimpleNamespace(tenant=tenant, user=None)

    with pytest.raises(HTTPException) as exc:
        await open_stripe_billing_portal(principal=principal)
    assert exc.value.status_code == 503
    assert "Stripe is not configured" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_stripe_portal_creates_customer_then_session(monkeypatch):
    """When the tenant has no stripe_customer_id, the endpoint must
    mint one before opening the portal session, and persist it back
    onto the tenant row."""
    from backend.app.api import stripe_webhook
    from backend.app.api.stripe_webhook import open_stripe_billing_portal

    monkeypatch.setattr(
        stripe_webhook,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_API_KEY="sk_test_xyz"),
    )

    posted = []

    async def fake_post(api_key, path, form):
        posted.append((path, form))
        if path == "/customers":
            return {"id": "cus_new123"}
        if path == "/billing_portal/sessions":
            assert form["customer"] == "cus_new123"
            assert form["return_url"].endswith("/billing")
            return {"url": "https://billing.stripe.com/p/session/abc"}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(stripe_webhook, "_stripe_post", fake_post)

    tenant = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        slug="acme",
        stripe_customer_id=None,
    )
    user = SimpleNamespace(email="admin@example.com")
    principal = SimpleNamespace(tenant=tenant, user=user)

    out = await open_stripe_billing_portal(principal=principal)
    assert out["portal_url"] == "https://billing.stripe.com/p/session/abc"
    # SPA reads ``url`` — both keys must be set to the same value.
    assert out["url"] == out["portal_url"]
    # The freshly minted customer id must be persisted.
    assert tenant.stripe_customer_id == "cus_new123"
    # Two POSTs in the right order: customer create, then portal session.
    assert [p for p, _ in posted] == ["/customers", "/billing_portal/sessions"]


@pytest.mark.asyncio
async def test_stripe_portal_reuses_existing_customer(monkeypatch):
    from backend.app.api import stripe_webhook
    from backend.app.api.stripe_webhook import open_stripe_billing_portal

    monkeypatch.setattr(
        stripe_webhook,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_API_KEY="sk_test_xyz"),
    )

    posted = []

    async def fake_post(api_key, path, form):
        posted.append(path)
        return {"url": "https://billing.stripe.com/p/session/zzz"}

    monkeypatch.setattr(stripe_webhook, "_stripe_post", fake_post)

    tenant = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        slug="acme",
        stripe_customer_id="cus_existing",
    )
    principal = SimpleNamespace(tenant=tenant, user=None)

    out = await open_stripe_billing_portal(principal=principal)
    assert out["portal_url"].startswith("https://billing.stripe.com/")
    # Skipped /customers entirely — only one POST.
    assert posted == ["/billing_portal/sessions"]
    # Customer id unchanged.
    assert tenant.stripe_customer_id == "cus_existing"
