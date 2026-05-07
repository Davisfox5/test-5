"""Scorecards API — CRUD for scorecard templates used in interaction evaluation."""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
    require_role,
    require_scope,
)
from backend.app.db import get_db
from backend.app.models import (
    InsightQualityScore,
    Interaction,
    ScorecardTemplate,
    Tenant,
)
from backend.app.services.audit_log import audit_log
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


class ReviewQueueItemOut(BaseModel):
    interaction_id: uuid.UUID
    title: Optional[str]
    channel: str
    status: str
    duration_seconds: Optional[int]
    created_at: datetime
    composite: Optional[float] = None
    weakest_dimension: Optional[str] = None
    weakest_score: Optional[float] = None
    sentiment_overall: Optional[str] = None
    churn_risk_signal: Optional[str] = None
    triage_priority: str  # high | medium | low

    model_config = {"from_attributes": True}


def _triage_band(
    composite: Optional[float],
    weakest: Optional[float],
    churn_risk_signal: Optional[str],
) -> str:
    """Research-derived triage:

    * **high** — composite well below threshold OR a single dimension
      critically low OR the call also has high churn risk (rep needs to
      know fast).
    * **medium** — only the composite or a single dimension below
      threshold.
    * **low** — borderline / training-data review only.
    """
    if churn_risk_signal == "high":
        return "high"
    if composite is not None and composite < 0.35:
        return "high"
    if weakest is not None and weakest < 0.30:
        return "high"
    if composite is not None and composite < 0.5:
        return "medium"
    if weakest is not None and weakest < 0.4:
        return "medium"
    return "low"


# IMPORTANT: registered BEFORE ``/scorecards/{template_id}`` so FastAPI's
# in-order route matching doesn't try to parse ``review-queue`` as a UUID.
@router.get("/scorecards/review-queue", response_model=List[ReviewQueueItemOut])
async def review_queue(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    triage: Optional[str] = Query(
        None, description="Filter by triage band: high | medium | low"
    ),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(require_role("manager")),
):
    """List interactions flagged for human review.

    Joins ``Interaction`` with the latest analysis-surface
    ``InsightQualityScore`` rows so the SPA can render composite +
    weakest-dimension + a research-derived triage band without per-row
    fan-out.
    """
    _ = principal
    stmt = (
        select(Interaction)
        .where(
            Interaction.tenant_id == tenant.id,
            Interaction.status == "flagged_for_review",
        )
        .order_by(Interaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    interactions = list((await db.execute(stmt)).scalars().all())
    if not interactions:
        return []

    ids = [ix.id for ix in interactions]
    score_stmt = (
        select(InsightQualityScore)
        .where(
            InsightQualityScore.tenant_id == tenant.id,
            InsightQualityScore.interaction_id.in_(ids),
            InsightQualityScore.surface == "analysis",
        )
        .order_by(InsightQualityScore.created_at.desc())
    )
    score_rows = list((await db.execute(score_stmt)).scalars().all())

    by_ix: Dict[uuid.UUID, Dict[str, float]] = {}
    for row in score_rows:
        if row.interaction_id is None:
            continue
        bucket = by_ix.setdefault(row.interaction_id, {})
        bucket.setdefault(row.dimension, row.score)

    out: List[ReviewQueueItemOut] = []
    for ix in interactions:
        scores = by_ix.get(ix.id, {})
        composite = scores.get("composite")
        per_dim = {k: v for k, v in scores.items() if k != "composite"}
        weakest_dim: Optional[str] = None
        weakest_score: Optional[float] = None
        if per_dim:
            weakest_dim = min(per_dim, key=per_dim.get)
            weakest_score = per_dim[weakest_dim]
        insights = ix.insights or {}
        churn = insights.get("churn_risk_signal")
        sentiment = insights.get("sentiment_overall")
        churn_str = churn if isinstance(churn, str) else None
        item = ReviewQueueItemOut(
            interaction_id=ix.id,
            title=ix.title,
            channel=ix.channel,
            status=ix.status,
            duration_seconds=ix.duration_seconds,
            created_at=ix.created_at,
            composite=composite,
            weakest_dimension=weakest_dim,
            weakest_score=weakest_score,
            sentiment_overall=sentiment if isinstance(sentiment, str) else None,
            churn_risk_signal=churn_str,
            triage_priority=_triage_band(composite, weakest_score, churn_str),
        )
        if triage and item.triage_priority != triage:
            continue
        out.append(item)
    return out


# Same ordering rule: this POST sits ahead of any
# ``/scorecards/{template_id}`` POST/PUT/DELETE, even though "resolve"
# couldn't collide today (UUID validation would 422), so the file stays
# safe if a future ``/scorecards/{template_id}/resolve`` is ever added.
@router.post(
    "/scorecards/review-queue/{interaction_id}/resolve",
    status_code=204,
    dependencies=[Depends(require_scope("scorecards:write"))],
)
async def resolve_review_queue_item(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(require_role("manager")),
):
    """Mark a flagged interaction as reviewed.

    Flips ``status`` back to ``analyzed`` so the queue clears. The
    underlying quality scores stay attached for trend reporting; this
    only resets the *gate* state.
    """
    stmt = select(Interaction).where(
        Interaction.id == interaction_id,
        Interaction.tenant_id == tenant.id,
    )
    interaction = (await db.execute(stmt)).scalar_one_or_none()
    if interaction is None:
        raise HTTPException(status_code=404, detail="Interaction not found")
    if interaction.status != "flagged_for_review":
        raise HTTPException(
            status_code=400,
            detail=f"Interaction is not flagged for review (status={interaction.status})",
        )
    before = {"status": interaction.status}
    interaction.status = "analyzed"
    await db.flush()
    await audit_log(
        db,
        principal,
        action="scorecard.review_resolved",
        resource_type="interaction",
        resource_id=str(interaction_id),
        before=before,
        after={"status": "analyzed"},
    )


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


@router.post(
    "/scorecards",
    response_model=ScorecardTemplateOut,
    status_code=201,
    dependencies=[Depends(require_scope("scorecards:write"))],
)
async def create_scorecard_template(
    body: ScorecardTemplateCreate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
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
    await audit_log(
        db,
        principal,
        action="scorecard.created",
        resource_type="scorecard",
        resource_id=str(template.id),
        after={"name": template.name, "is_default": template.is_default},
    )
    return template


@router.put(
    "/scorecards/{template_id}",
    response_model=ScorecardTemplateOut,
    dependencies=[Depends(require_scope("scorecards:write"))],
)
async def update_scorecard_template(
    template_id: uuid.UUID,
    body: ScorecardTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    stmt = select(ScorecardTemplate).where(
        ScorecardTemplate.id == template_id,
        ScorecardTemplate.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Scorecard template not found")

    before = {"name": template.name, "is_default": template.is_default}
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

    await db.flush()
    await audit_log(
        db,
        principal,
        action="scorecard.updated",
        resource_type="scorecard",
        resource_id=str(template.id),
        before=before,
        after={"name": template.name, "is_default": template.is_default},
    )
    return template


@router.delete(
    "/scorecards/{template_id}",
    status_code=204,
    dependencies=[Depends(require_scope("scorecards:write"))],
)
async def delete_scorecard_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    stmt = select(ScorecardTemplate).where(
        ScorecardTemplate.id == template_id,
        ScorecardTemplate.tenant_id == tenant.id,
    )
    result = await db.execute(stmt)
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Scorecard template not found")
    snapshot = {"name": template.name, "is_default": template.is_default}
    await db.delete(template)
    await db.flush()
    await audit_log(
        db,
        principal,
        action="scorecard.deleted",
        resource_type="scorecard",
        resource_id=str(template_id),
        before=snapshot,
    )
