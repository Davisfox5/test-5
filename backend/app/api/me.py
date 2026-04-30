"""Current-tenant / current-user context for the SPA.

Returns everything the frontend needs on boot to decide what to render:
identity, plan tier, trial state, feature limits, and Ask-Linda
availability. Maps the ``TierSpec`` returned by ``limits_for(tenant)``
into the flat-feature shape the SPA's ``useMe()`` consumes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.app.auth import AuthPrincipal, get_current_principal
from backend.app.plans import TierSpec, limits_for, trial_is_active, trial_is_expired

router = APIRouter()


class PlanLimitsOut(BaseModel):
    real_time_transcription: bool
    live_coaching: bool
    crm_push: bool
    custom_scorecards: bool
    custom_branding: bool
    ask_linda: bool
    api_access: bool
    max_users: Optional[int]
    max_monthly_minutes: Optional[int]
    max_uploads_per_day: Optional[int]
    # Pin the literal so the OpenAPI client + the SPA's narrowed
    # `"haiku"|"sonnet"|"opus"` union stay in lockstep. A future tier
    # ("haiku-fast") needs to widen this on both sides — silently drifting
    # would break the SPA's exhaustive-switch type-checks.
    ai_model_tier: Literal["haiku", "sonnet", "opus"]


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan_tier: str
    is_white_label: bool
    trial_ends_at: Optional[datetime]
    trial_active: bool
    trial_expired: bool
    # True iff the tenant has an active Stripe subscription. The SPA
    # uses this to decide between the first-time plan picker (false)
    # and the Stripe billing portal (true) on /billing.
    has_subscription: bool
    limits: PlanLimitsOut


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: Optional[str]
    role: str


class MeOut(BaseModel):
    tenant: TenantOut
    user: Optional[UserOut]


def _plan_limits_out(spec: TierSpec) -> PlanLimitsOut:
    """Project ``TierSpec`` into the flat shape the SPA expects.

    ``TierSpec.features`` is a free-form dict; the API surfaces only
    the seven gates the dashboard actually reads (UI-facing toggles).
    Adding a new gate? Add it both here and in
    ``apps/app/src/lib/me.ts:PlanLimits``.
    """
    f = spec.features
    return PlanLimitsOut(
        real_time_transcription=bool(f.get("real_time_transcription", False)),
        live_coaching=bool(f.get("live_coaching", False)),
        crm_push=bool(f.get("crm_push", False)),
        custom_scorecards=bool(f.get("custom_scorecards", False)),
        custom_branding=bool(f.get("custom_branding", False)),
        ask_linda=bool(f.get("ask_linda", False)),
        api_access=bool(f.get("api_access", False)),
        max_users=spec.seat_limit,
        max_monthly_minutes=spec.max_monthly_minutes,
        max_uploads_per_day=spec.max_uploads_per_day,
        ai_model_tier=spec.ai_model_tier,
    )


@router.get("/me", response_model=MeOut)
async def me(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> MeOut:
    tenant = principal.tenant
    user = principal.user
    return MeOut(
        tenant=TenantOut(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            plan_tier=tenant.plan_tier,
            is_white_label=tenant.is_white_label,
            trial_ends_at=tenant.trial_ends_at,
            trial_active=trial_is_active(tenant),
            trial_expired=trial_is_expired(tenant),
            # Treat a whitespace-only id as "no subscription" — guards
            # against a stale string that survived a cancel webhook.
            has_subscription=bool(
                tenant.stripe_subscription_id
                and tenant.stripe_subscription_id.strip()
            ),
            limits=_plan_limits_out(limits_for(tenant)),
        ),
        user=UserOut(
            id=user.id, email=user.email, name=user.name, role=user.role
        )
        if user is not None
        else None,
    )
