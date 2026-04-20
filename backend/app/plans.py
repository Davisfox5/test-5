"""Pricing-tier / plan feature gates.

Single source of truth for what every plan tier can and cannot do. The
frontend reads ``/api/v1/me`` to discover the active tenant's limits and
hides or disables UI accordingly; the backend uses ``require_feature`` as
a FastAPI dependency to reject locked requests with 402.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import Depends, HTTPException

from backend.app.auth import get_current_user_or_tenant
from backend.app.models import Tenant

PLAN_TIERS = ("sandbox", "starter", "growth", "enterprise")


@dataclass(frozen=True)
class PlanLimits:
    """Per-tier feature flags and caps. Frontend mirrors these in useMe()."""

    # Functional gates
    real_time_transcription: bool
    live_coaching: bool
    crm_push: bool
    custom_scorecards: bool
    custom_branding: bool
    ask_linda: bool
    api_access: bool
    # Seats + usage caps (None = unlimited)
    max_users: Optional[int]
    max_monthly_minutes: Optional[int]
    max_uploads_per_day: Optional[int]
    # Model tier for analysis — "haiku" | "sonnet" | "opus"
    ai_model_tier: str


PLANS: Dict[str, PlanLimits] = {
    "sandbox": PlanLimits(
        real_time_transcription=False,
        live_coaching=False,
        crm_push=False,
        custom_scorecards=False,
        custom_branding=False,
        ask_linda=True,
        api_access=False,
        max_users=3,
        max_monthly_minutes=120,
        max_uploads_per_day=10,
        ai_model_tier="haiku",
    ),
    "starter": PlanLimits(
        real_time_transcription=False,
        live_coaching=False,
        crm_push=True,
        custom_scorecards=False,
        custom_branding=False,
        ask_linda=True,
        api_access=True,
        max_users=10,
        max_monthly_minutes=2000,
        max_uploads_per_day=None,
        ai_model_tier="sonnet",
    ),
    "growth": PlanLimits(
        real_time_transcription=True,
        live_coaching=True,
        crm_push=True,
        custom_scorecards=True,
        custom_branding=False,
        ask_linda=True,
        api_access=True,
        max_users=50,
        max_monthly_minutes=10_000,
        max_uploads_per_day=None,
        ai_model_tier="sonnet",
    ),
    "enterprise": PlanLimits(
        real_time_transcription=True,
        live_coaching=True,
        crm_push=True,
        custom_scorecards=True,
        custom_branding=True,
        ask_linda=True,
        api_access=True,
        max_users=None,
        max_monthly_minutes=None,
        max_uploads_per_day=None,
        ai_model_tier="opus",
    ),
}


def limits_for(tenant: Tenant) -> PlanLimits:
    return PLANS.get(tenant.plan_tier, PLANS["sandbox"])


def trial_is_active(tenant: Tenant) -> bool:
    if tenant.plan_tier != "sandbox" or tenant.trial_ends_at is None:
        return False
    return tenant.trial_ends_at > datetime.now(timezone.utc)


def trial_is_expired(tenant: Tenant) -> bool:
    return (
        tenant.plan_tier == "sandbox"
        and tenant.trial_ends_at is not None
        and tenant.trial_ends_at <= datetime.now(timezone.utc)
    )


# ── FastAPI dependency factory ────────────────────────────────────────────


def require_feature(flag: str):
    """Return a FastAPI dependency that 402s if the current tenant lacks ``flag``.

    Usage:

        @router.post("/calls/live", dependencies=[Depends(require_feature("real_time_transcription"))])
        async def start_live_call(...): ...
    """

    async def _guard(tenant: Tenant = Depends(get_current_user_or_tenant)) -> Tenant:
        limits = limits_for(tenant)
        if not getattr(limits, flag, False):
            raise HTTPException(
                status_code=402,
                detail=f"Your plan does not include '{flag}'. Upgrade to unlock.",
            )
        if trial_is_expired(tenant):
            raise HTTPException(
                status_code=402,
                detail="Your sandbox trial has ended. Pick a plan to keep going.",
            )
        return tenant

    return _guard
