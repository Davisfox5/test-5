"""Profile API — expose the latest client/agent/manager/business profile.

Access rules (RBAC, tenant-scoped on top of every query):

* ``agent`` — own ``AgentProfile``; ``ClientProfile`` only for contacts
  the agent has handled (``Interaction.agent_id == me``).
* ``manager`` — own ``ManagerProfile``; ``AgentProfile`` / ``ClientProfile``
  for agents where ``User.manager_id == me`` (and their clients).
* ``admin`` — everything within the tenant, including the
  ``BusinessProfile``.

The payload returned is the *public* projection of the versioned profile
row: summary narrative, structured metrics, top-K factors, and top
recommendations. Raw β coefficients, calibration parameters, and the
full feature vector are deliberately never exposed through this surface.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, get_current_principal
from backend.app.db import get_db
from backend.app.models import (
    AgentProfile,
    BusinessProfile,
    ClientProfile,
    Interaction,
    ManagerProfile,
    Tenant,
    User,
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
    """Public projection of a profile row."""

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
    cfg = getattr(tenant, "branding_config", {}) or {}
    if cfg.get("expert_mode_enabled"):
        return 10
    return 3


def _project(row: Any, entity_id: uuid.UUID, tenant: Tenant) -> ProfileOut:
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


# ── RBAC gates ───────────────────────────────────────────────────────────


def _forbidden() -> HTTPException:
    return HTTPException(status_code=403, detail="Forbidden")


async def _authorize_agent_access(
    db: AsyncSession, principal: AuthPrincipal, agent_id: uuid.UUID
) -> None:
    """Raise 403 unless the caller can view ``agent_id``'s profile.

    * admin → always.
    * manager → agent is themselves OR manages this agent (User.manager_id).
    * agent → agent is themselves.
    """
    if principal.role == "admin":
        return
    me = principal.user_id
    if me is not None and me == agent_id:
        return
    if principal.role == "manager" and me is not None:
        stmt = select(exists().where(
            User.id == agent_id,
            User.tenant_id == principal.tenant.id,
            User.manager_id == me,
        ))
        if bool((await db.execute(stmt)).scalar()):
            return
    raise _forbidden()


async def _authorize_client_access(
    db: AsyncSession, principal: AuthPrincipal, contact_id: uuid.UUID
) -> None:
    """Raise 403 unless the caller can view ``contact_id``'s client profile.

    * admin → always.
    * agent → at least one Interaction with agent_id == me + contact_id == target.
    * manager → at least one Interaction on this contact where the handling
      agent is one of the manager's reports (or is themselves).
    """
    if principal.role == "admin":
        return
    me = principal.user_id
    if me is None:
        raise _forbidden()
    if principal.role == "agent":
        stmt = select(exists().where(
            Interaction.tenant_id == principal.tenant.id,
            Interaction.contact_id == contact_id,
            Interaction.agent_id == me,
        ))
        if bool((await db.execute(stmt)).scalar()):
            return
        raise _forbidden()
    if principal.role == "manager":
        # Manager passes if any interaction on this contact was handled by
        # one of their reports (or by the manager themselves).
        reports_stmt = select(User.id).where(
            User.tenant_id == principal.tenant.id,
            User.manager_id == me,
        )
        reports = [uid for (uid,) in (await db.execute(reports_stmt)).all()]
        allowed = set(reports) | {me}
        stmt = select(exists().where(
            Interaction.tenant_id == principal.tenant.id,
            Interaction.contact_id == contact_id,
            Interaction.agent_id.in_(allowed),
        ))
        if bool((await db.execute(stmt)).scalar()):
            return
        raise _forbidden()
    raise _forbidden()


async def _authorize_manager_access(
    principal: AuthPrincipal, manager_id: uuid.UUID
) -> None:
    """Raise 403 unless the caller can view ``manager_id``'s profile.

    Managers can view only their own profile; admins can view any manager
    in the tenant.
    """
    if principal.role == "admin":
        return
    if principal.role == "manager" and principal.user_id == manager_id:
        return
    raise _forbidden()


async def _authorize_business_access(principal: AuthPrincipal) -> None:
    """The business profile is admin-only — it aggregates across the
    whole tenant and exposes trends agents/managers shouldn't necessarily
    see outside of curated dashboards."""
    if principal.role != "admin":
        raise _forbidden()


# ── Client profile ───────────────────────────────────────────────────────


@router.get("/profiles/clients/{contact_id}", response_model=ProfileOut)
async def get_client_profile(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    await _authorize_client_access(db, principal, contact_id)
    row = await _latest(db, ClientProfile, "contact_id", contact_id)
    if row is None or row.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Client profile not found")
    return _project(row, contact_id, principal.tenant)


@router.get(
    "/profiles/clients/{contact_id}/history",
    response_model=List[ProfileHistoryOut],
)
async def client_profile_history(
    contact_id: uuid.UUID,
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    await _authorize_client_access(db, principal, contact_id)
    rows = await _history(db, ClientProfile, "contact_id", contact_id, limit)
    return [
        ProfileHistoryOut(
            version=r.version,
            as_of=(r.profile or {}).get("as_of"),
            headline=(r.profile or {}).get("summary", "")[:120],
        )
        for r in rows
        if r.tenant_id == principal.tenant.id
    ]


# ── Agent profile ────────────────────────────────────────────────────────


@router.get("/profiles/agents/{agent_id}", response_model=ProfileOut)
async def get_agent_profile(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    await _authorize_agent_access(db, principal, agent_id)
    row = await _latest(db, AgentProfile, "agent_id", agent_id)
    if row is None or row.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    return _project(row, agent_id, principal.tenant)


# ── Manager profile ──────────────────────────────────────────────────────


@router.get("/profiles/managers/{manager_id}", response_model=ProfileOut)
async def get_manager_profile(
    manager_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    await _authorize_manager_access(principal, manager_id)
    row = await _latest(db, ManagerProfile, "manager_id", manager_id)
    if row is None or row.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Manager profile not found")
    return _project(row, manager_id, principal.tenant)


# ── Business profile ─────────────────────────────────────────────────────


@router.get("/profiles/business", response_model=ProfileOut)
async def get_business_profile(
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    await _authorize_business_access(principal)
    row = await _latest(db, BusinessProfile, "business_tenant_id", principal.tenant.id)
    if row is None:
        raise HTTPException(status_code=404, detail="Business profile not yet generated")
    return _project(row, principal.tenant.id, principal.tenant)


@router.get(
    "/profiles/business/history",
    response_model=List[ProfileHistoryOut],
)
async def business_profile_history(
    limit: int = Query(12, ge=1, le=52),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    await _authorize_business_access(principal)
    rows = await _history(
        db, BusinessProfile, "business_tenant_id", principal.tenant.id, limit
    )
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
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Compute fresh scores for one interaction from the feature store.

    Access: the interaction must belong to the caller's tenant, AND the
    caller must pass the agent/manager gate that fits the role — agents
    can only see their own interactions; managers see their reports';
    admins see anything in the tenant.
    """
    from backend.app.models import InteractionFeatures
    from backend.app.services import score_engine

    interaction = await db.get(Interaction, interaction_id)
    if interaction is None or interaction.tenant_id != principal.tenant.id:
        raise HTTPException(status_code=404, detail="Interaction not found")
    if principal.role == "agent":
        if interaction.agent_id != principal.user_id:
            raise _forbidden()
    elif principal.role == "manager":
        me = principal.user_id
        if interaction.agent_id != me:
            reports_stmt = select(User.id).where(
                User.tenant_id == principal.tenant.id,
                User.manager_id == me,
            )
            reports = {uid for (uid,) in (await db.execute(reports_stmt)).all()}
            if interaction.agent_id not in reports:
                raise _forbidden()

    stmt = (
        select(InteractionFeatures)
        .where(
            InteractionFeatures.interaction_id == interaction_id,
            InteractionFeatures.tenant_id == principal.tenant.id,
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
        (getattr(principal.tenant, "branding_config", {}) or {}).get("expert_mode_enabled")
    )

    sentiment_result = score_engine.default_sentiment_scorer().score(
        score_engine.flatten_features_for_sentiment(features)
    )
    churn_result = score_engine.default_churn_scorer().score(
        score_engine.flatten_features_for_churn(features)
    )
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
