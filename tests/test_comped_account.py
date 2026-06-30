"""Comped-account entitlements — davison@flexonline.net gets full free access.

A comped tenant must behave as top-tier (enterprise) everywhere, never count
as trial-expired, and bypass the subscription/billing gate — regardless of its
actual plan_tier, trial_ends_at, or stripe_subscription_id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.app import plans
from backend.app.services import entitlements


def _tenant(*, comped=False, plan_tier="sandbox", trial_ends_at=None,
            stripe_subscription_id=None):
    t = SimpleNamespace(
        plan_tier=plan_tier,
        trial_ends_at=trial_ends_at,
        stripe_subscription_id=stripe_subscription_id,
    )
    entitlements.mark_tenant_comped(t, comped)
    return t


PAST = datetime.now(timezone.utc) - timedelta(days=1)


# ── allowlist primitives ────────────────────────────────


def test_email_is_comped_case_insensitive():
    assert entitlements.email_is_comped("davison@flexonline.net")
    assert entitlements.email_is_comped("  DAVISON@FlexOnline.net ")
    assert not entitlements.email_is_comped("someone@else.com")
    assert not entitlements.email_is_comped(None)
    assert not entitlements.email_is_comped("")


def test_mark_and_read_flag():
    t = SimpleNamespace()
    assert entitlements.tenant_is_comped(t) is False  # default
    entitlements.mark_tenant_comped(t, True)
    assert entitlements.tenant_is_comped(t) is True


# ── limits_for ──────────────────────────────────────────


def test_comped_tenant_gets_enterprise_limits():
    comped = _tenant(comped=True, plan_tier="sandbox")
    spec = plans.limits_for(comped)
    assert spec.key == "enterprise"
    # Enterprise has every gated feature on; sandbox would not.
    assert spec.features["real_time_transcription"] is True
    assert spec.features["live_coaching"] is True


def test_non_comped_sandbox_gets_sandbox_limits():
    plain = _tenant(comped=False, plan_tier="sandbox")
    spec = plans.limits_for(plain)
    assert spec.key == "sandbox"
    assert spec.features["real_time_transcription"] is False


# ── trial expiry ────────────────────────────────────────


def test_comped_never_trial_expired_even_with_past_trial():
    comped = _tenant(comped=True, plan_tier="sandbox", trial_ends_at=PAST)
    assert plans.trial_is_expired(comped) is False


def test_non_comped_sandbox_past_trial_is_expired():
    plain = _tenant(comped=False, plan_tier="sandbox", trial_ends_at=PAST)
    assert plans.trial_is_expired(plain) is True


# ── require_feature guard ───────────────────────────────


@pytest.mark.asyncio
async def test_require_feature_passes_for_comped_on_locked_flag():
    guard = plans.require_feature("live_coaching")
    comped = _tenant(comped=True, plan_tier="sandbox", trial_ends_at=PAST)
    assert await guard(tenant=comped) is comped  # no 402


@pytest.mark.asyncio
async def test_require_feature_402s_for_non_comped_sandbox():
    from fastapi import HTTPException

    guard = plans.require_feature("live_coaching")
    plain = _tenant(comped=False, plan_tier="sandbox")
    with pytest.raises(HTTPException) as exc:
        await guard(tenant=plain)
    assert exc.value.status_code == 402


# ── require_active_subscription guard ───────────────────


@pytest.mark.asyncio
async def test_subscription_gate_bypassed_for_comped():
    # Comped on enterprise with NO stripe sub would normally 402; must pass.
    comped = _tenant(comped=True, plan_tier="enterprise", stripe_subscription_id=None)
    assert await plans.require_active_subscription(tenant=comped) is comped
    # Comped sandbox with an expired trial must also pass.
    comped2 = _tenant(comped=True, plan_tier="sandbox", trial_ends_at=PAST)
    assert await plans.require_active_subscription(tenant=comped2) is comped2


@pytest.mark.asyncio
async def test_subscription_gate_402s_for_non_comped_expired():
    from fastapi import HTTPException

    plain = _tenant(comped=False, plan_tier="sandbox", trial_ends_at=PAST)
    with pytest.raises(HTTPException) as exc:
        await plans.require_active_subscription(tenant=plain)
    assert exc.value.status_code == 402
