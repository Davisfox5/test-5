"""Experiment + prompt-variant management API.

Used by the platform-team admin tool to:
- List / create prompt variants
- Promote variants (Gate 2: ready_for_review → active)
- Roll back variants
- List / create experiments
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Experiment, PromptVariant, Tenant
from backend.app.services.prompt_variant_service import bust_cache

router = APIRouter()


# ── Variants ─────────────────────────────────────────────


class VariantCreate(BaseModel):
    name: str
    description: Optional[str] = None
    prompt_template: str
    target_surface: str
    target_tier: Optional[str] = None
    target_channel: Optional[str] = None
    parent_variant_id: Optional[uuid.UUID] = None


class VariantOut(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    target_surface: str
    target_tier: Optional[str]
    target_channel: Optional[str]
    version: int
    status: str
    parent_variant_id: Optional[uuid.UUID]
    created_at: datetime
    retired_at: Optional[datetime]


@router.get("/prompt-variants", response_model=List[VariantOut])
async def list_variants(
    surface: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(PromptVariant).order_by(PromptVariant.created_at.desc())
    if surface:
        stmt = stmt.where(PromptVariant.target_surface == surface)
    if status:
        stmt = stmt.where(PromptVariant.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [VariantOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/prompt-variants", response_model=VariantOut, status_code=201)
async def create_variant(
    body: VariantCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    parent_version = 1
    if body.parent_variant_id is not None:
        parent = (
            await db.execute(
                select(PromptVariant).where(PromptVariant.id == body.parent_variant_id)
            )
        ).scalar_one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent variant not found")
        parent_version = parent.version + 1
    variant = PromptVariant(
        name=body.name,
        description=body.description,
        prompt_template=body.prompt_template,
        target_surface=body.target_surface,
        target_tier=body.target_tier,
        target_channel=body.target_channel,
        parent_variant_id=body.parent_variant_id,
        version=parent_version,
        status="draft",
    )
    db.add(variant)
    await db.flush()
    return VariantOut.model_validate(variant, from_attributes=True)


class VariantTransition(BaseModel):
    status: str = Field(..., description="'shadow'|'canary'|'active'|'rolled_back'|'retired'")


_ALLOWED_TRANSITIONS = {
    "draft": {"shadow", "canary", "active", "retired"},
    "shadow": {"canary", "active", "rolled_back", "retired"},
    "canary": {"shadow", "active", "rolled_back", "retired"},
    "active": {"rolled_back", "retired"},
    "ready_for_review": {"active", "rolled_back", "retired"},
    "rolled_back": {"shadow", "canary", "active", "retired"},
    "retired": set(),
}


@router.post("/prompt-variants/{variant_id}/transition", response_model=VariantOut)
async def transition_variant(
    variant_id: uuid.UUID,
    body: VariantTransition,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Move a variant through the rollout lifecycle (Gate 2 — admin only).

    Promoting to ``active`` automatically demotes the prior active variant
    for the same ``(surface, tier, channel)`` triplet to ``retired`` so we
    don't end up with two active rows.
    """
    variant = (
        await db.execute(select(PromptVariant).where(PromptVariant.id == variant_id))
    ).scalar_one_or_none()
    if variant is None:
        raise HTTPException(status_code=404, detail="Variant not found")
    allowed = _ALLOWED_TRANSITIONS.get(variant.status, set())
    if body.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot transition {variant.status} → {body.status}",
        )

    if body.status == "active":
        # Retire the prior active variant for this routing tuple.
        await db.execute(
            PromptVariant.__table__.update()
            .where(
                PromptVariant.target_surface == variant.target_surface,
                PromptVariant.target_tier == variant.target_tier,
                PromptVariant.target_channel == variant.target_channel,
                PromptVariant.status == "active",
                PromptVariant.id != variant.id,
            )
            .values(status="retired", retired_at=datetime.utcnow())
        )

    variant.status = body.status
    if body.status == "retired":
        variant.retired_at = datetime.utcnow()
    bust_cache()
    return VariantOut.model_validate(variant, from_attributes=True)


# ── Experiments ──────────────────────────────────────────


class ExperimentCreate(BaseModel):
    name: str
    type: str
    surface: Optional[str] = None
    hypothesis: Optional[str] = None
    control_variant_id: Optional[uuid.UUID] = None
    treatment_variant_id: Optional[uuid.UUID] = None


class ExperimentOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    surface: Optional[str]
    status: str
    hypothesis: Optional[str]
    control_variant_id: Optional[uuid.UUID]
    treatment_variant_id: Optional[uuid.UUID]
    start_date: datetime
    end_date: Optional[datetime]
    result_summary: dict
    conclusion: Optional[str]
    created_at: datetime


@router.get("/experiments", response_model=List[ExperimentOut])
async def list_experiments(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    stmt = select(Experiment).order_by(Experiment.start_date.desc())
    if status:
        stmt = stmt.where(Experiment.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [ExperimentOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/experiments", response_model=ExperimentOut, status_code=201)
async def create_experiment(
    body: ExperimentCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    exp = Experiment(
        name=body.name,
        type=body.type,
        surface=body.surface,
        hypothesis=body.hypothesis,
        control_variant_id=body.control_variant_id,
        treatment_variant_id=body.treatment_variant_id,
    )
    db.add(exp)
    await db.flush()
    return ExperimentOut.model_validate(exp, from_attributes=True)


@router.post("/experiments/{exp_id}/conclude", response_model=ExperimentOut)
async def conclude_experiment(
    exp_id: uuid.UUID,
    conclusion: str,
    decided_by: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    exp = (
        await db.execute(select(Experiment).where(Experiment.id == exp_id))
    ).scalar_one_or_none()
    if exp is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    exp.status = "concluded"
    exp.conclusion = conclusion
    exp.end_date = datetime.utcnow()
    exp.decided_by = decided_by
    return ExperimentOut.model_validate(exp, from_attributes=True)
