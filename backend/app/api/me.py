"""Current-tenant / current-user context for the SPA.

Returns everything the frontend needs on boot to decide what to render:
identity, plan tier, trial state, feature limits, and Ask-Linda
availability. Mirrors the ``PlanLimits`` shape from ``backend.app.plans``.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.app.auth import AuthPrincipal, get_current_principal
from backend.app.plans import limits_for, trial_is_active, trial_is_expired

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
    ai_model_tier: str


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan_tier: str
    is_white_label: bool
    trial_ends_at: Optional[datetime]
    trial_active: bool
    trial_expired: bool
    limits: PlanLimitsOut


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: Optional[str]
    role: str


class MeOut(BaseModel):
    tenant: TenantOut
    user: Optional[UserOut]


@router.get("/me", response_model=MeOut)
async def me(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> MeOut:
    tenant = principal.tenant
    user = principal.user
    limits = limits_for(tenant)
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
            limits=PlanLimitsOut(**asdict(limits)),
        ),
        user=UserOut(
            id=user.id, email=user.email, name=user.name, role=user.role
        )
        if user is not None
        else None,
    )
