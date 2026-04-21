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

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Tenant, User
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


async def _resolve_current_user(
    request: Request, db: AsyncSession, tenant: Tenant
) -> Optional[User]:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token.startswith("clerk_"):
        return None
    stmt = select(User).where(User.clerk_user_id == token, User.tenant_id == tenant.id)
    return (await db.execute(stmt)).scalar_one_or_none()


@router.get("/me", response_model=MeOut)
async def me(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> MeOut:
    user = await _resolve_current_user(request, db, tenant)
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
