"""Targeted tests for the polish-layer fixes shipped under audit-nice-to-haves.

Covers four load-bearing changes:

* Stripe webhook secret rotation — accepts old or new during overlap.
* Feedback POST rate limit — 429 once the per-tenant ceiling is hit.
* Per-tenant retention overrides on the event_retention sweep — tenants
  with a custom threshold prune at their own window; everyone else falls
  through to the system default.
* OAuth provider listing — Zoho / Microsoft Dynamics surface as
  ``certified=False`` so the SPA renders them as "Coming soon" without
  letting users start the (uncertified) flow.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio


# ── Stripe webhook secret rotation ──────────────────────────────────


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_rotation_accepts_old_secret():
    from backend.app.services.stripe_billing import (
        verify_stripe_signature_with_rotation,
    )

    body = b'{"type":"customer.subscription.updated"}'
    ts = int(time.time())
    sig = _sign("whsec_old", ts, body)
    header = f"t={ts},v1={sig}"
    assert verify_stripe_signature_with_rotation(
        payload_bytes=body,
        signature_header=header,
        secrets=["whsec_old", "whsec_new"],
    )


def test_rotation_accepts_new_secret():
    from backend.app.services.stripe_billing import (
        verify_stripe_signature_with_rotation,
    )

    body = b'{"type":"customer.subscription.updated"}'
    ts = int(time.time())
    sig = _sign("whsec_new", ts, body)
    header = f"t={ts},v1={sig}"
    assert verify_stripe_signature_with_rotation(
        payload_bytes=body,
        signature_header=header,
        secrets=["whsec_old", "whsec_new"],
    )


def test_rotation_rejects_unknown_secret():
    from backend.app.services.stripe_billing import (
        verify_stripe_signature_with_rotation,
    )

    body = b"x"
    ts = int(time.time())
    sig = _sign("whsec_attacker", ts, body)
    header = f"t={ts},v1={sig}"
    assert not verify_stripe_signature_with_rotation(
        payload_bytes=body,
        signature_header=header,
        secrets=["whsec_old", "whsec_new"],
    )


def test_rotation_skips_blank_secret_slots():
    """A blank ``..._NEXT`` env var must not trip the verifier."""
    from backend.app.services.stripe_billing import (
        verify_stripe_signature_with_rotation,
    )

    body = b"x"
    ts = int(time.time())
    sig = _sign("whsec_only", ts, body)
    header = f"t={ts},v1={sig}"
    assert verify_stripe_signature_with_rotation(
        payload_bytes=body,
        signature_header=header,
        secrets=["whsec_only", ""],
    )


# ── Feedback rate limit ─────────────────────────────────────────────


def test_feedback_rate_limit_blocks_after_ceiling(monkeypatch):
    """Once the per-tenant feedback ceiling fires, the limiter returns False."""
    from backend.app.services.push_rate_limiter import RateLimiter

    # Force the local-bucket fallback so the test doesn't hit Redis.
    rl = RateLimiter()
    monkeypatch.setattr(rl, "_get_redis", lambda: None)

    key = f"feedback:{uuid.uuid4()}"
    limit = 60

    for _ in range(limit):
        allowed, _, _ = rl.check(key=key, limit=limit, window_seconds=60)
        assert allowed is True

    # Limit + 1th request must be denied.
    allowed, remaining, reset = rl.check(key=key, limit=limit, window_seconds=60)
    assert allowed is False
    assert remaining == 0
    assert reset >= 0


def test_feedback_rate_limit_keys_are_per_tenant(monkeypatch):
    """One tenant exhausting its bucket must not affect another tenant."""
    from backend.app.services.push_rate_limiter import RateLimiter

    rl = RateLimiter()
    monkeypatch.setattr(rl, "_get_redis", lambda: None)

    a = f"feedback:{uuid.uuid4()}"
    b = f"feedback:{uuid.uuid4()}"
    limit = 5

    for _ in range(limit):
        rl.check(key=a, limit=limit, window_seconds=60)

    blocked_for_a, _, _ = rl.check(key=a, limit=limit, window_seconds=60)
    fresh_for_b, _, _ = rl.check(key=b, limit=limit, window_seconds=60)
    assert blocked_for_a is False
    assert fresh_for_b is True


# ── Per-tenant retention thresholds ─────────────────────────────────


@pytest_asyncio.fixture
async def two_tenants(test_session_factory):
    """Seed two tenants — one with a custom feedback retention override."""
    from backend.app.models import Tenant

    async with test_session_factory() as session:
        custom = Tenant(
            name="Custom Retention",
            slug=f"t-{uuid.uuid4().hex[:8]}",
            retention_days_feedback_events=30,
            retention_days_webhook_deliveries=7,
        )
        default = Tenant(
            name="Default Retention",
            slug=f"t-{uuid.uuid4().hex[:8]}",
        )
        session.add_all([custom, default])
        await session.commit()
        await session.refresh(custom)
        await session.refresh(default)
        return custom, default


@pytest.mark.asyncio
async def test_per_tenant_webhook_delivery_retention(
    test_session_factory, two_tenants
):
    """Tenant override prunes earlier than the global default."""
    from backend.app.models import Webhook, WebhookDelivery
    from backend.app.services.event_retention import sweep_webhook_deliveries

    custom, default = two_tenants
    now = datetime.now(timezone.utc)

    async with test_session_factory() as session:
        # One webhook row each so the FK constraint is satisfied.
        wh_custom = Webhook(
            tenant_id=custom.id,
            url="https://example.com/c",
            secret="s",
            events=["*"],
        )
        wh_default = Webhook(
            tenant_id=default.id,
            url="https://example.com/d",
            secret="s",
            events=["*"],
        )
        session.add_all([wh_custom, wh_default])
        await session.flush()

        # 30-day-old delivery for each tenant — past the custom 7d window
        # but inside the default 90d window.
        old = now - timedelta(days=30)
        session.add_all(
            [
                WebhookDelivery(
                    tenant_id=custom.id,
                    webhook_id=wh_custom.id,
                    event="t",
                    status="sent",
                    created_at=old,
                ),
                WebhookDelivery(
                    tenant_id=default.id,
                    webhook_id=wh_default.id,
                    event="t",
                    status="sent",
                    created_at=old,
                ),
            ]
        )
        await session.commit()

    async with test_session_factory() as session:
        deleted = await sweep_webhook_deliveries(session)

    # Only the custom tenant's row exceeds its 7d window.
    assert deleted == 1


@pytest.mark.asyncio
async def test_per_tenant_feedback_overrides_loaded(
    test_session_factory, two_tenants
):
    """The override-lookup helper returns only tenants that set a value.

    The rollup path itself uses Postgres-only ``func.date()`` semantics,
    so we only exercise the per-tenant override read here — the bulk-pass
    behavior is identical to the webhook-delivery sweep tested above.
    """
    from backend.app.models import Tenant
    from backend.app.services.event_retention import _tenant_retention_overrides

    custom, default = two_tenants
    async with test_session_factory() as session:
        overrides = await _tenant_retention_overrides(
            session, Tenant.retention_days_feedback_events
        )

    # ``custom`` has an explicit value; ``default`` does not, so it must
    # not appear — the bulk pass would handle it under the system default.
    assert custom.id in overrides
    assert default.id not in overrides
    assert overrides[custom.id] == 30


# ── OAuth provider listing ─────────────────────────────────────────


def test_oauth_provider_stubs_are_marked_uncertified():
    from backend.app.api.oauth import CRM_PROVIDERS, _is_certified

    assert "zoho" in CRM_PROVIDERS
    assert "microsoft_dynamics" in CRM_PROVIDERS
    assert _is_certified("zoho") is False
    assert _is_certified("microsoft_dynamics") is False
    # Existing CRM providers stay certified.
    assert _is_certified("hubspot") is True
    assert _is_certified("salesforce") is True
    assert _is_certified("pipedrive") is True


def test_oauth_authorize_refuses_uncertified():
    """``_require_certified`` must raise on stubs so we never start a
    flow that the (missing) adapter can't finish.
    ``_validate_provider`` keeps accepting them so revoke/cleanup paths
    on stale rows still work.
    """
    from fastapi import HTTPException

    from backend.app.api.oauth import _require_certified, _validate_provider

    # Validation alone is permissive — stubs are still recognised providers.
    _validate_provider("zoho")  # must not raise
    _validate_provider("microsoft_dynamics")  # must not raise
    # Flow-start gate rejects stubs.
    with pytest.raises(HTTPException) as exc:
        _require_certified("zoho")
    assert exc.value.status_code == 400
