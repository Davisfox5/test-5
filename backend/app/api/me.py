"""Current-tenant / current-user context for the SPA.

Returns everything the frontend needs on boot to decide what to render:
identity, plan tier, trial state, feature limits, and Ask-Linda
availability. Maps the ``TierSpec`` returned by ``limits_for(tenant)``
into the flat-feature shape the SPA's ``useMe()`` consumes.

Also hosts the sandbox-only preview-role switcher
(``POST /me/preview-role``). The override is a render-time overlay
gated at two layers (sandbox tier + role validity); the underlying
``users.role`` column is never mutated by this endpoint, so the
override never relaxes a security boundary. The trial-active gate
that used to wrap this was removed so a sandbox tenant can keep
previewing roles after the 14-day window — useful when a buying
decision drags past the trial. See the principal resolver in
:mod:`backend.app.auth` for the load-bearing gate.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    _tenant_allows_role_preview,
    get_current_principal,
)
from backend.app.db import get_db
from backend.app.models import User
from backend.app.plans import TierSpec, limits_for, trial_is_active, trial_is_expired
from backend.app.services.audit_log import audit_log

router = APIRouter()


PreviewRole = Literal["agent", "manager", "admin"]


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
    # True iff this tenant may render the role-preview pill regardless
    # of tier. Mirrors the backend predicate so the SPA doesn't have to
    # reimplement "sandbox OR override-on" in two places.
    role_preview_enabled: bool
    limits: PlanLimitsOut


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: Optional[str]
    # The *effective* role: the preview-role overlay if it's currently
    # being applied, otherwise the user's real ``users.role``. Sidebar
    # nav + role-gated UI should read this directly.
    role: str
    # The user's stored override (``users.preview_role``). Surfaced so
    # the switcher can render which option is checked.
    preview_role: Optional[PreviewRole]
    # The user's underlying ``users.role`` (no preview overlay). Lets
    # the SPA render "Switch back to admin" when the preview differs.
    real_role: str
    # True iff the principal resolver applied the preview overlay on
    # this request — drives the "you're in preview mode" banner.
    is_previewing: bool


class MeOut(BaseModel):
    tenant: TenantOut
    user: Optional[UserOut]


class PreviewRoleIn(BaseModel):
    """Body for ``POST /me/preview-role``.

    ``role: null`` clears the override. Anything outside the literal
    set is rejected by Pydantic with HTTP 422.
    """

    role: Optional[PreviewRole] = None


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


def _user_out(principal: AuthPrincipal) -> Optional[UserOut]:
    user = principal.user
    if user is None:
        return None
    raw_preview = user.preview_role
    preview_role: Optional[PreviewRole] = (
        raw_preview if raw_preview in {"agent", "manager", "admin"} else None
    )
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=principal.role,
        preview_role=preview_role,
        real_role=principal.real_role,
        is_previewing=principal.is_previewing,
    )


@router.get("/me", response_model=MeOut)
async def me(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> MeOut:
    tenant = principal.tenant
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
            role_preview_enabled=_tenant_allows_role_preview(tenant),
            limits=_plan_limits_out(limits_for(tenant)),
        ),
        user=_user_out(principal),
    )


@router.post("/me/preview-role")
async def set_preview_role(
    body: PreviewRoleIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
) -> Dict[str, Any]:
    """Set or clear the calling user's preview role.

    Allowed on sandbox tenants or any tenant with
    ``role_preview_enabled = True``. The override is render-time only —
    the user's real role is unchanged and remains the source of truth
    for security. ``role: null`` clears.
    """
    # API-key callers don't have a human user behind them; preview is
    # an interactive-session feature only.
    if principal.source == "api_key" or principal.user is None:
        raise HTTPException(
            status_code=403,
            detail="preview role only applies to interactive sessions",
        )

    tenant = principal.tenant
    if not _tenant_allows_role_preview(tenant):
        raise HTTPException(
            status_code=403,
            detail="preview role is not enabled for this tenant",
        )

    # Look the user up in *this* request's DB session — the principal's
    # ``.user`` came from the principal-resolver's session and isn't
    # attached here, so mutations on it wouldn't reach this commit.
    db_user = (
        await db.execute(select(User).where(User.id == principal.user.id))
    ).scalar_one()

    before_value = db_user.preview_role
    new_value = body.role  # Pydantic already validated the literal set.

    db_user.preview_role = new_value
    await audit_log(
        db,
        principal,
        action="user.preview_role_set",
        resource_type="user",
        resource_id=str(db_user.id),
        before={"preview_role": before_value},
        after={"preview_role": new_value},
    )
    await db.commit()

    return {
        "role": new_value,
        "real_role": db_user.role or "agent",
    }
