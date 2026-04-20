"""Profile API — expose the latest client/agent/manager/business profile.

The payload returned is the *public* projection of the versioned profile
row: summary narrative, structured metrics, top-K factors, and top
recommendations.  Raw β coefficients, calibration parameters, and the
full feature vector are deliberately never exposed through this surface.

Access control matches each entity's ownership model: agents can read
their own profile plus the profiles of clients they've touched; managers
can read their team's agents and those agents' clients; tenant admins
can read the business profile.  The current implementation leaves
per-user scoping as a TODO (no user-authz middleware yet in this
codebase) and scopes strictly by tenant.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    AgentProfile,
    BusinessProfile,
    ClientProfile,
    ManagerProfile,
    Tenant,
)

router = APIRouter()


# ── Response schemas ─────────────────────────────────────────────────────


class FactorOut(BaseModel):
    label: str
    direction: str  # "+" or "-"
    magnitude_pct: float
    why: Optional[str] = None


class RecommendationOut(BaseModel):
    action: str
    priority: str
    expected_impact: Optional[str] = None


class ProfileOut(BaseModel):
    """Public projection of a profile row.

    Only ``metrics`` that the profile explicitly published are included;
    nothing leaks raw feature vectors or scorer weights.
    """

    entity_id: uuid.UUID
    version: int
    as_of: Optional[str]
    summary: str
    metrics: Dict[str, Any]
    top_factors: List[FactorOut]
    recommendations: List[RecommendationOut]
    confidence: Optional[float]


class ProfileHistoryOut(BaseModel):
    version: int
    as_of: Optional[str]
    headline: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _factor_cap(tenant: Tenant) -> int:
    """Return the max number of factors to expose for this tenant.

    Default 3; tenants with ``expert_mode_enabled`` may see up to 10.
    The feature flag is stored in ``Tenant.branding_config`` for now to
    avoid a new column on the already-large tenants table.
    """
    cfg = getattr(tenant, "branding_config", {}) or {}
    if cfg.get("expert_mode_enabled"):
        return 10
    return 3


def _project(row: Any, entity_id: uuid.UUID, tenant: Tenant) -> ProfileOut:
    """Cast a profile ORM row into its public projection."""
    profile = row.profile or {}
    factors = (row.top_factors or [])[: _factor_cap(tenant)]
    recommendations = (profile.get("recommendations") or [])[:3]
    return ProfileOut(
        entity_id=entity_id,
        version=row.version,
        as_of=profile.get("as_of"),
        summary=profile.get("summary", ""),
        metrics=profile.get("metrics", {}),
        top_factors=[FactorOut(**f) for f in factors],
        recommendations=[RecommendationOut(**r) for r in recommendations],
        confidence=row.confidence,
    )


async def _latest(db: AsyncSession, model, fk: str, entity_id: uuid.UUID) -> Optional[Any]:
    stmt = (
        select(model)
        .where(getattr(model, fk) == entity_id)
        .order_by(desc(model.version))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _history(db: AsyncSession, model, fk: str, entity_id: uuid.UUID, limit: int) -> List[Any]:
    stmt = (
        select(model)
        .where(getattr(model, fk) == entity_id)
        .order_by(desc(model.version))
        .limit(limit)
    )
    return (await db.execute(stmt)).scalars().all()


# ── Client profile ───────────────────────────────────────────────────────


@router.get("/profiles/clients/{contact_id}", response_model=ProfileOut)
async def get_client_profile(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Latest ClientProfile for one contact (scoped to caller's tenant)."""
    row = await _latest(db, ClientProfile, "contact_id", contact_id)
    if row is None or row.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Client profile not found")
    return _project(row, contact_id, tenant)


@router.get(
    "/profiles/clients/{contact_id}/history",
    response_model=List[ProfileHistoryOut],
)
async def client_profile_history(
    contact_id: uuid.UUID,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    rows = await _history(db, ClientProfile, "contact_id", contact_id, limit)
    return [
        ProfileHistoryOut(
            version=r.version,
            as_of=(r.profile or {}).get("as_of"),
            headline=(r.profile or {}).get("summary", "")[:120],
        )
        for r in rows
        if r.tenant_id == tenant.id
    ]


# ── Agent profile ────────────────────────────────────────────────────────


@router.get("/profiles/agents/{agent_id}", response_model=ProfileOut)
async def get_agent_profile(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    row = await _latest(db, AgentProfile, "agent_id", agent_id)
    if row is None or row.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    return _project(row, agent_id, tenant)


# ── Manager profile ──────────────────────────────────────────────────────


@router.get("/profiles/managers/{manager_id}", response_model=ProfileOut)
async def get_manager_profile(
    manager_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    row = await _latest(db, ManagerProfile, "manager_id", manager_id)
    if row is None or row.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Manager profile not found")
    return _project(row, manager_id, tenant)


# ── Business profile ─────────────────────────────────────────────────────


@router.get("/profiles/business", response_model=ProfileOut)
async def get_business_profile(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    row = await _latest(db, BusinessProfile, "business_tenant_id", tenant.id)
    if row is None:
        raise HTTPException(status_code=404, detail="Business profile not yet generated")
    return _project(row, tenant.id, tenant)


@router.get(
    "/profiles/business/history",
    response_model=List[ProfileHistoryOut],
)
async def business_profile_history(
    limit: int = Query(12, ge=1, le=52),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    rows = await _history(db, BusinessProfile, "business_tenant_id", tenant.id, limit)
    return [
        ProfileHistoryOut(
            version=r.version,
            as_of=(r.profile or {}).get("as_of"),
            headline=(r.profile or {}).get("summary", "")[:120],
        )
        for r in rows
    ]


# ── Interaction-level aggregate scores ───────────────────────────────────


class InteractionScoreOut(BaseModel):
    interaction_id: uuid.UUID
    sentiment: Dict[str, Any]
    churn_risk: Dict[str, Any]
    health_indicators: Dict[str, Any]


@router.get(
    "/interactions/{interaction_id}/scores",
    response_model=InteractionScoreOut,
)
async def interaction_scores(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Compute fresh scores for one interaction from the feature store.

    Returns three aggregates (sentiment, churn risk, per-call health
    indicators) each with top-K factors and recommendations.  This is
    a read-only derivation — no LLM calls — so it is cheap and fast.
    """
    from backend.app.models import InteractionFeatures
    from backend.app.services import score_engine

    stmt = (
        select(InteractionFeatures)
        .where(
            InteractionFeatures.interaction_id == interaction_id,
            InteractionFeatures.tenant_id == tenant.id,
        )
    )
    features_row = (await db.execute(stmt)).scalar_one_or_none()
    if features_row is None:
        raise HTTPException(status_code=404, detail="Interaction features not found")

    features = {
        "deterministic": features_row.deterministic or {},
        "llm_structured": features_row.llm_structured or {},
    }
    expert = bool(
        (getattr(tenant, "branding_config", {}) or {}).get("expert_mode_enabled")
    )

    sentiment_result = score_engine.default_sentiment_scorer().score(
        score_engine.flatten_features_for_sentiment(features)
    )
    churn_result = score_engine.default_churn_scorer().score(
        score_engine.flatten_features_for_churn(features)
    )
    # Per-call health indicator combines sentiment + interaction quality.
    # We reuse the default health composite; missing fields simply drop
    # out of the factor list and reduce confidence.
    health_inputs = {
        "sentiment_delta_vs_baseline": (features_row.deterministic or {}).get("sentiment_trajectory_slope"),
        "stakeholder_count": (features_row.deterministic or {}).get("stakeholder_count"),
        "response_latency_p90": None,
        "scorecard_score": None,
        "action_item_completion_rate": None,
        "competitor_pressure": len(
            (features_row.llm_structured or {}).get("competitor_mentions") or []
        ),
    }
    health_result = score_engine.default_health_scorer().score(health_inputs)

    return InteractionScoreOut(
        interaction_id=interaction_id,
        sentiment=sentiment_result.to_public(expert_mode=expert),
        churn_risk=churn_result.to_public(expert_mode=expert),
        health_indicators=health_result.to_public(expert_mode=expert),
    )
