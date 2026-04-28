"""Tests for the audit-criticals PR.

Covers items where the change is non-trivial:

* #4 — webhook secret encryption on insert + decryption on dispatch.
* #5 — comment author = current principal (not stub UUID).
* #7 — Stripe webhook expansion: customer.created, invoice.payment_failed
       (with the 3-strike auto-downgrade), invoice.payment_succeeded.
* #8 — ``require_active_subscription`` 402 on expired sandbox / lapsed
       paid tenants, 200 on healthy tenants.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio


# ── Item 4: Webhook secret encryption ───────────────────────────────


def test_encrypt_token_actually_obfuscates_secret():
    """Encrypted output must not equal the plaintext secret."""
    from backend.app.services.token_crypto import encrypt_token, decrypt_token

    plaintext = "test-webhook-secret-do-not-leak"
    encrypted = encrypt_token(plaintext)
    assert encrypted is not None
    assert encrypted != plaintext
    # Round-trip must recover the original.
    assert decrypt_token(encrypted) == plaintext


def test_decrypt_token_tolerates_legacy_plaintext():
    """Rows written before the rollout aren't Fernet ciphertext.

    The dispatcher needs to keep delivering them with the legacy
    plaintext value rather than 500-ing on InvalidToken.
    """
    from backend.app.services.token_crypto import decrypt_token

    legacy = "legacy-plaintext-from-before-encryption"
    assert decrypt_token(legacy) == legacy


def test_encrypt_token_idempotent_on_already_encrypted():
    """Double-encrypt branch must round-trip cleanly."""
    from backend.app.services.token_crypto import encrypt_token, decrypt_token

    plaintext = "abc"
    once = encrypt_token(plaintext)
    twice = encrypt_token(once)
    assert once == twice
    assert decrypt_token(twice) == plaintext


# ── Item 7: Stripe webhook expansion ───────────────────────────────


@pytest.mark.asyncio
async def test_handle_customer_created_links_tenant_when_metadata_present(
    test_session_factory,
):
    """``customer.created`` with metadata.tenant_id pins the customer id."""
    from backend.app.api.stripe_webhook import _handle_customer_created
    from backend.app.models import Tenant

    async with test_session_factory() as session:
        tenant = Tenant(name="Linkable Co", slug=f"t-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

        result = await _handle_customer_created(
            session,
            {
                "id": "cus_link_me",
                "metadata": {"tenant_id": str(tenant.id)},
            },
        )
        assert result["handled"] is True
        assert result["linked"] is True
        assert result["stripe_customer_id"] == "cus_link_me"

        # The handler mutates the ORM instance fetched via ``db.get``
        # which is a different identity than ``tenant`` here; verify by
        # querying back fresh.
        await session.commit()
        from sqlalchemy import select as _select
        from backend.app.models import Tenant as _Tenant
        refetched = (
            await session.execute(_select(_Tenant).where(_Tenant.id == tenant.id))
        ).scalar_one()
        assert refetched.stripe_customer_id == "cus_link_me"


@pytest.mark.asyncio
async def test_handle_customer_created_does_not_clobber_existing_link(
    test_session_factory,
):
    """A stray customer.created shouldn't overwrite a tenant's existing link."""
    from backend.app.api.stripe_webhook import _handle_customer_created
    from backend.app.models import Tenant

    async with test_session_factory() as session:
        tenant = Tenant(
            name="Already Linked",
            slug=f"t-{uuid.uuid4().hex[:8]}",
            stripe_customer_id="cus_original",
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

        result = await _handle_customer_created(
            session,
            {
                "id": "cus_replacement",
                "metadata": {"tenant_id": str(tenant.id)},
            },
        )
        assert result["handled"] is True
        assert result["linked"] is False

        await session.refresh(tenant)
        assert tenant.stripe_customer_id == "cus_original"


@pytest.mark.asyncio
async def test_payment_failed_increments_streak_and_downgrades_after_third(
    test_session_factory,
):
    """Three consecutive ``invoice.payment_failed`` events flip to sandbox."""
    from backend.app.api.stripe_webhook import _handle_payment_failed
    from backend.app.models import Tenant

    async with test_session_factory() as session:
        tenant = Tenant(
            name="Failing Payer",
            slug=f"t-{uuid.uuid4().hex[:8]}",
            stripe_customer_id="cus_fail",
            stripe_subscription_id="sub_active",
            plan_tier="growth",
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

        # First failure — counter -> 1, no downgrade.
        # Reconcile_seats writes to the DB; mock it out so the test
        # doesn't need a fully migrated schema for unrelated tables.
        with patch(
            "backend.app.api.stripe_webhook.reconcile_seats",
            new=AsyncMock(return_value=None),
        ):
            r1 = await _handle_payment_failed(
                session, {"customer": "cus_fail", "id": "in_1"}
            )
            assert r1["consecutive_failures"] == 1
            assert r1["downgraded"] is False
            assert tenant.plan_tier == "growth"

            r2 = await _handle_payment_failed(
                session, {"customer": "cus_fail", "id": "in_2"}
            )
            assert r2["consecutive_failures"] == 2
            assert r2["downgraded"] is False
            assert tenant.plan_tier == "growth"

            # Third failure — downgrade fires.
            r3 = await _handle_payment_failed(
                session, {"customer": "cus_fail", "id": "in_3"}
            )
            assert r3["consecutive_failures"] == 3
            assert r3["downgraded"] is True
            assert tenant.plan_tier == "sandbox"


@pytest.mark.asyncio
async def test_payment_succeeded_clears_failure_streak(test_session_factory):
    """A successful invoice resets the consecutive-failure counter."""
    from backend.app.api.stripe_webhook import (
        _PAYMENT_FAILURE_AT_KEY,
        _PAYMENT_FAILURE_KEY,
        _handle_payment_succeeded,
    )
    from backend.app.models import Tenant

    async with test_session_factory() as session:
        tenant = Tenant(
            name="Recovered Payer",
            slug=f"t-{uuid.uuid4().hex[:8]}",
            stripe_customer_id="cus_ok",
            features_enabled={
                _PAYMENT_FAILURE_KEY: 2,
                _PAYMENT_FAILURE_AT_KEY: "2026-04-28T00:00:00+00:00",
            },
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

        result = await _handle_payment_succeeded(
            session, {"customer": "cus_ok", "id": "in_paid"}
        )
        assert result["handled"] is True
        assert _PAYMENT_FAILURE_KEY not in (tenant.features_enabled or {})


# ── Item 8: require_active_subscription ─────────────────────────────


@pytest.mark.asyncio
async def test_require_active_subscription_passes_for_in_trial_sandbox():
    from backend.app.models import Tenant
    from backend.app.plans import require_active_subscription

    tenant = Tenant(
        name="Trial Co",
        slug="trial-co",
        plan_tier="sandbox",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    # Should not raise.
    out = await require_active_subscription(tenant=tenant)
    assert out is tenant


@pytest.mark.asyncio
async def test_require_active_subscription_402_for_expired_trial():
    from fastapi import HTTPException

    from backend.app.models import Tenant
    from backend.app.plans import require_active_subscription

    tenant = Tenant(
        name="Expired Co",
        slug="expired-co",
        plan_tier="sandbox",
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    with pytest.raises(HTTPException) as ctx:
        await require_active_subscription(tenant=tenant)
    assert ctx.value.status_code == 402


@pytest.mark.asyncio
async def test_require_active_subscription_402_for_paid_tier_without_subscription():
    from fastapi import HTTPException

    from backend.app.models import Tenant
    from backend.app.plans import require_active_subscription

    tenant = Tenant(
        name="Lapsed Co",
        slug="lapsed-co",
        plan_tier="growth",
        # No stripe_subscription_id linked → not "active".
    )
    with pytest.raises(HTTPException) as ctx:
        await require_active_subscription(tenant=tenant)
    assert ctx.value.status_code == 402


@pytest.mark.asyncio
async def test_require_active_subscription_passes_for_paid_tier_with_subscription():
    from backend.app.models import Tenant
    from backend.app.plans import require_active_subscription

    tenant = Tenant(
        name="Paying Co",
        slug="paying-co",
        plan_tier="growth",
        stripe_subscription_id="sub_active",
    )
    out = await require_active_subscription(tenant=tenant)
    assert out is tenant


# ── Item 5: Comment author resolution ───────────────────────────────


def test_comment_module_no_longer_exposes_demo_user_id():
    """The DEMO_USER_ID stub must be gone — it stamped every comment with
    the same fake user id and broke audit + FKs."""
    from backend.app.api import comments as comments_mod

    assert not hasattr(comments_mod, "DEMO_USER_ID")
    assert not hasattr(comments_mod, "get_current_user_id")
