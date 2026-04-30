"""Scorecards API — CRUD for scorecard templates used in interaction evaluation."""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import ScorecardTemplate, Tenant
from backend.app.services.scorecard_entitlement import compute_entitlement

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class ScorecardTemplateCreate(BaseModel):
    name: str
    criteria: List[Dict]
    channel_filter: Optional[List[str]] = None
    is_default: bool = False


class ScorecardTemplateOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    criteria: List[Dict]
    channel_filter: Optional[List[str]]
    is_default: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ScorecardTemplateUpdate(BaseModel):
    name: Optional[str] = None
    criteria: Optional[List[Dict]] = None
    channel_filter: Optional[List[str]] = None
    is_default: Optional[bool] = None


# ── Endpoints ────────────────────────────────────────────


@router.get("/scorecards", response_model=List[ScorecardTemplateOut])
async def list_scorecard_templates(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = (
        select(ScorecardTemplate)
        .where(ScorecardTemplate.tenant_id == tenant.id)
        .order_by(ScorecardTemplate.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/scorecards/{template_id}", response_model=ScorecardTemplateOut)
async def get_scorecard_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Tenant-scoped detail for a single scorecard template."""
    stmt = select(ScorecardTemplate).where(
        ScorecardTemplate.id == template_id,
        ScorecardTemplate.tenant_id == tenant.id,
    )
    template = (await db.execute(stmt)).scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Scorecard template not found")
    return template


@router.post("/scorecards", response_model=ScorecardTemplateOut, status_code=201)
async def create_scorecard_template(
    body: ScorecardTemplateCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    # Entitlement gate: each tenant gets ``ceil(seats/10)`` included
    # scorecards (one per admin seat), plus any *Extra Scorecard* add-on
    # subscription items the tenant has bought via Stripe. Block creation
    # at the cap with HTTP 402 so the SPA can prompt for an upgrade.
    entitlement = await compute_entitlement(db, tenant)
    if entitlement.used >= entitlement.total:
        raise HTTPException(
            status_code=402,
            detail={
                "detail": "Scorecard cap reached",
                "limit": entitlement.total,
                "current": entitlement.used,
                "included": entitlement.included,
                "paid_extra": entitlement.paid_extra,
            },
        )

    # If this template is marked as default, unset any existing defaults
    if body.is_default:
        existing_defaults_stmt = (
            select(ScorecardTemplate)
            .where(ScorecardTemplate.tenant_id == tenant.id, ScorecardTemplate.is_default.is_(True))
        )
        existing_result = await db.execute(existing_defaults_stmt)
        for existing in existing_result.scalars().all():
            existing.is_default = False

    template = ScorecardTemplate(
        tenant_id=tenant.id,
        name=body.name,
        criteria=body.criteria,
        channel_filter=body.channel_filter,
        is_default=body.is_default,
    )
    db.add(template)
    await db.flush()
    return template


@router.put("/scorecards/{template_id}", response_model=ScorecardTemplateOut)
async def update_scorecard_template(
    template_id: uuid.UUID,
    body: ScorecardTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(ScorecardTemplate).where(
        ScorecardTemplate.id == template_id,
        ScorecardTemplate.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Scorecard template not found")

    if body.name is not None:
        template.name = body.name
    if body.criteria is not None:
        template.criteria = body.criteria
    if body.channel_filter is not None:
        template.channel_filter = body.channel_filter
    if body.is_default is not None:
        # If setting as default, unset other defaults
        if body.is_default:
            existing_defaults_stmt = (
                select(ScorecardTemplate)
                .where(
                    ScorecardTemplate.tenant_id == tenant.id,
                    ScorecardTemplate.is_default.is_(True),
                    ScorecardTemplate.id != template_id,
                )
            )
            existing_result = await db.execute(existing_defaults_stmt)
            for existing in existing_result.scalars().all():
                existing.is_default = False
        template.is_default = body.is_default

    return template


@router.delete("/scorecards/{template_id}", status_code=204)
async def delete_scorecard_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(ScorecardTemplate).where(
        ScorecardTemplate.id == template_id,
        ScorecardTemplate.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Scorecard template not found")
    await db.delete(template)
