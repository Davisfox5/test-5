"""Public signup surfaces: demo-email capture + 14-day sandbox trial."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db import get_db
from backend.app.models import DemoEmailCapture, Tenant, User

router = APIRouter()

TRIAL_DAYS = 14
SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    slug = SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "tenant"


# ── Public demo email capture ─────────────────────────────────────────────


class DemoEmailCaptureIn(BaseModel):
    email: EmailStr
    source: Optional[str] = "public-demo"
    utm: dict = Field(default_factory=dict)


class DemoEmailCaptureOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/demo/email-capture", response_model=DemoEmailCaptureOut)
async def capture_demo_email(
    payload: DemoEmailCaptureIn, db: AsyncSession = Depends(get_db)
) -> DemoEmailCaptureOut:
    """Unauthenticated — the public demo POSTs here after the 60s gate trips."""
    row = DemoEmailCapture(email=payload.email, source=payload.source, utm=payload.utm)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# ── 14-day sandbox trial signup ───────────────────────────────────────────


class TrialSignupIn(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    company: str = Field(min_length=2, max_length=255)
    clerk_user_id: Optional[str] = None
    # Optional onboarding metadata captured by the SPA's signup form.
    # Stored on the tenant for downstream personalization + analytics —
    # never used as auth signal.
    role: Optional[str] = Field(default=None, max_length=120)
    company_size: Optional[str] = Field(default=None, max_length=32)
    use_case: Optional[str] = Field(default=None, max_length=64)


class TrialSignupOut(BaseModel):
    tenant_id: uuid.UUID
    tenant_slug: str
    user_id: uuid.UUID
    plan_tier: str
    trial_ends_at: datetime


@router.post("/trial/signup", response_model=TrialSignupOut)
async def trial_signup(
    payload: TrialSignupIn, db: AsyncSession = Depends(get_db)
) -> TrialSignupOut:
    """Create a sandbox tenant + the signing-up user with a 14-day trial clock."""
    # If the same Clerk user already finished signup, return their existing
    # tenant instead of allocating a duplicate (handles double-submits and
    # the /signup/complete page being reloaded).
    if payload.clerk_user_id:
        existing_user = (
            await db.execute(
                select(User).where(User.clerk_user_id == payload.clerk_user_id)
            )
        ).scalar_one_or_none()
        if existing_user is not None:
            tenant_row = (
                await db.execute(
                    select(Tenant).where(Tenant.id == existing_user.tenant_id)
                )
            ).scalar_one()
            return TrialSignupOut(
                tenant_id=tenant_row.id,
                tenant_slug=tenant_row.slug,
                user_id=existing_user.id,
                plan_tier=tenant_row.plan_tier,
                trial_ends_at=tenant_row.trial_ends_at or datetime.now(timezone.utc),
            )

    base_slug = _slugify(payload.company)
    slug = base_slug
    for attempt in range(1, 25):
        existing = (
            await db.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if existing is None:
            break
        slug = f"{base_slug}-{attempt}"
    else:
        raise HTTPException(status_code=409, detail="Could not allocate a tenant slug")

    trial_ends_at = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
    onboarding: dict = {}
    if payload.role:
        onboarding["role"] = payload.role
    if payload.company_size:
        onboarding["company_size"] = payload.company_size
    if payload.use_case:
        onboarding["use_case"] = payload.use_case
    tenant = Tenant(
        name=payload.company,
        slug=slug,
        plan_tier="sandbox",
        trial_ends_at=trial_ends_at,
        tenant_context={"onboarding": onboarding} if onboarding else {},
    )
    db.add(tenant)
    await db.flush()

    user = User(
        tenant_id=tenant.id,
        clerk_user_id=payload.clerk_user_id,
        email=payload.email,
        name=payload.name,
        role="executive",  # first user on a tenant gets the top role by default
    )
    db.add(user)
    await db.flush()

    # If this email was previously captured by the public demo, link it forward.
    await db.execute(
        DemoEmailCapture.__table__.update()
        .where(DemoEmailCapture.email == payload.email, DemoEmailCapture.converted_tenant_id.is_(None))
        .values(converted_tenant_id=tenant.id)
    )
    await db.commit()

    return TrialSignupOut(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        user_id=user.id,
        plan_tier=tenant.plan_tier,
        trial_ends_at=trial_ends_at,
    )
