"""Manager dashboard — narrative-first 10,000-foot view.

Replaces the prior ``manager_dashboard.py`` router. The new surface is
organized around a headline narrative drawn from ``BusinessProfile``,
a live anomaly feed (``ManagerAlert``), a recommendation queue
(``ManagerRecommendation``) with one-click apply, and the legacy deep
signals (talk/listen, churn throughput, methodology adherence,
training gap) preserved as drill-downs.

All routes are gated by ``require_role("manager")``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Float as sa_Float
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    AuthPrincipal,
    get_current_principal,
    get_current_tenant,
    require_role,
)
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import (
    ActionItem,
    AlertChannelConfig,
    AlertDomainConfig,
    BusinessProfile,
    Campaign,
    CoachingNote,
    Customer,
    Interaction,
    ManagerAlert,
    ManagerRecommendation,
    Tenant,
    User,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────
# Pydantic response/request shapes
# ─────────────────────────────────────────────────────────────────────


class NarrativeOut(BaseModel):
    as_of: Optional[datetime]
    summary: str
    top_factors: List[Any] = Field(default_factory=list)
    confidence: Optional[float]
    version: int
    playbook_insights: Dict[str, Any] = Field(default_factory=dict)


class AlertOut(BaseModel):
    id: uuid.UUID
    kind: str
    severity: str
    title: str
    body: Optional[str]
    evidence: Dict[str, Any]
    domain: Optional[str]
    opened_at: datetime
    acknowledged_at: Optional[datetime]
    dismissed_at: Optional[datetime]
    resolved_at: Optional[datetime]


class RecommendationOut(BaseModel):
    id: uuid.UUID
    category: str
    title: str
    rationale: Optional[str]
    # Composed account brief from the enrichment pass:
    # {"headline": str, "sections": [{"kind", "title", "body"?, "items"?}]}.
    # None until enrichment runs (rationale is the fallback text).
    brief: Optional[Dict[str, Any]] = None
    enriched_at: Optional[datetime] = None
    evidence: Dict[str, Any]
    target: Dict[str, Any]
    score: float
    status: str
    domain: Optional[str]
    applied_artifact_type: Optional[str]
    applied_artifact_id: Optional[uuid.UUID]
    expires_at: datetime
    created_at: datetime


class DismissRequest(BaseModel):
    reason: Optional[str] = None


class ApplyResult(BaseModel):
    artifact_type: str
    artifact_id: uuid.UUID


class AlertConfigOut(BaseModel):
    inapp_enabled: bool
    slack_enabled: bool
    slack_min_severity: str
    topic_spike_pct_change_threshold: Optional[int]
    topic_spike_min_volume: Optional[int]
    sentiment_drop_threshold: Optional[float]
    churn_surge_multiplier: Optional[float]
    methodology_drop_threshold: Optional[float]


class AlertConfigUpdate(BaseModel):
    inapp_enabled: Optional[bool] = None
    slack_enabled: Optional[bool] = None
    slack_min_severity: Optional[str] = None
    topic_spike_pct_change_threshold: Optional[int] = None
    topic_spike_min_volume: Optional[int] = None
    sentiment_drop_threshold: Optional[float] = None
    churn_surge_multiplier: Optional[float] = None
    methodology_drop_threshold: Optional[float] = None


class RepTalkListenRow(BaseModel):
    rep_id: Optional[str]
    rep_name: Optional[str]
    call_count: int
    talk_pct_avg: Optional[float]


class TalkListenDistribution(BaseModel):
    rows: List[RepTalkListenRow]
    tenant_avg_talk_pct: Optional[float]


class ChurnThroughputBucket(BaseModel):
    bucket: str
    count: int


class ChurnThroughput(BaseModel):
    window_days: int
    buckets: List[ChurnThroughputBucket]
    total_calls: int


class MethodologyAdherence(BaseModel):
    framework: str
    total_calls: int
    avg_coverage_ratio: Optional[float]
    most_missed_stage: Optional[str]


class ManagerOverview(BaseModel):
    window_days: int
    talk_listen: TalkListenDistribution
    churn_throughput: ChurnThroughput
    methodology: List[MethodologyAdherence]


class RepTrainingGap(BaseModel):
    rep_id: Optional[str]
    rep_name: Optional[str]
    call_count: int
    reflection_rate: Optional[float]
    open_question_rate: Optional[float]
    avg_methodology_coverage: Optional[float]


class TrainingGapReport(BaseModel):
    window_days: int
    rows: List[RepTrainingGap]


# ─────────────────────────────────────────────────────────────────────
# Narrative
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/manager/narrative",
    response_model=NarrativeOut,
    dependencies=[Depends(require_role("manager"))],
)
async def get_narrative(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Latest BusinessProfile + tenant playbook_insights — narrative card data."""
    row = await _latest_business_profile(db, tenant.id)
    profile = (row.profile if row else {}) or {}
    playbook = (tenant.tenant_context or {}).get("playbook_insights") or {}
    return NarrativeOut(
        as_of=row.created_at if row else None,
        summary=profile.get("summary", ""),
        top_factors=(row.top_factors if row else []) or [],
        confidence=row.confidence if row else None,
        version=row.version if row else 0,
        playbook_insights=playbook,
    )


@router.post(
    "/manager/narrative/refresh",
    dependencies=[Depends(require_role("manager"))],
)
async def refresh_narrative(
    tenant: Tenant = Depends(get_current_tenant),
):
    """Force a BusinessProfile refresh. Rate-limited 1/hr/tenant via Redis
    so Opus cost stays bounded if a manager taps the button repeatedly.
    """
    if not _claim_refresh_slot(tenant.id):
        raise HTTPException(
            status_code=429,
            detail="Narrative refresh already ran in the last hour for this tenant.",
        )
    try:
        from backend.app.tasks import orchestrator_daily_one_tenant

        async_result = orchestrator_daily_one_tenant.delay(str(tenant.id))
        return {"enqueued": True, "task_id": async_result.id}
    except Exception as exc:
        logger.exception("Narrative refresh enqueue failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/manager/alerts",
    response_model=List[AlertOut],
    dependencies=[Depends(require_role("manager"))],
)
async def list_alerts(
    only_open: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    domain: Optional[str] = Query(
        None,
        description=(
            "Restrict to one motion (sales | customer_service | "
            "it_support | generic). Unset returns every domain — used "
            "by the cross-motion Journey view."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    if domain is not None and domain not in {
        "sales",
        "customer_service",
        "it_support",
        "generic",
    }:
        raise HTTPException(status_code=422, detail=f"Unknown domain: {domain}")
    stmt = select(ManagerAlert).where(ManagerAlert.tenant_id == tenant.id)
    if only_open:
        stmt = stmt.where(
            ManagerAlert.acknowledged_at.is_(None),
            ManagerAlert.dismissed_at.is_(None),
            ManagerAlert.resolved_at.is_(None),
        )
    if domain is not None:
        # Treat NULL on the row as "sales" so pre-``dom_002`` legacy
        # alerts still appear under their motion tab on a sales-only
        # tenant. After backfill there shouldn't be any NULL rows in
        # production, but keep the safety net.
        if domain == "sales":
            stmt = stmt.where(
                (ManagerAlert.domain == "sales") | ManagerAlert.domain.is_(None)
            )
        else:
            stmt = stmt.where(ManagerAlert.domain == domain)
    stmt = stmt.order_by(desc(ManagerAlert.opened_at)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        AlertOut(
            id=r.id,
            kind=r.kind,
            severity=r.severity,
            title=r.title,
            body=r.body,
            evidence=r.evidence or {},
            domain=r.domain,
            opened_at=r.opened_at,
            acknowledged_at=r.acknowledged_at,
            dismissed_at=r.dismissed_at,
            resolved_at=r.resolved_at,
        )
        for r in rows
    ]


@router.post(
    "/manager/alerts/{alert_id}/acknowledge",
    dependencies=[Depends(require_role("manager"))],
)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    alert = await _get_tenant_alert(db, tenant.id, alert_id)
    alert.acknowledged_at = datetime.now(timezone.utc)
    alert.acknowledged_by_user_id = principal.user_id
    await db.commit()
    return {"ok": True}


@router.post(
    "/manager/alerts/{alert_id}/dismiss",
    dependencies=[Depends(require_role("manager"))],
)
async def dismiss_alert(
    alert_id: uuid.UUID,
    body: DismissRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    alert = await _get_tenant_alert(db, tenant.id, alert_id)
    alert.dismissed_at = datetime.now(timezone.utc)
    alert.dismiss_reason = body.reason
    await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# Recommendations
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/manager/recommendations",
    response_model=List[RecommendationOut],
    dependencies=[Depends(require_role("manager"))],
)
async def list_recommendations(
    status: str = Query("open"),
    limit: int = Query(20, ge=1, le=100),
    domain: Optional[str] = Query(
        None,
        description=(
            "Restrict to one motion. Unset returns every domain."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    if domain is not None and domain not in {
        "sales",
        "customer_service",
        "it_support",
        "generic",
    }:
        raise HTTPException(status_code=422, detail=f"Unknown domain: {domain}")
    stmt = (
        select(ManagerRecommendation)
        .where(
            ManagerRecommendation.tenant_id == tenant.id,
            ManagerRecommendation.status == status,
        )
        .order_by(desc(ManagerRecommendation.score))
        .limit(limit)
    )
    if domain is not None:
        if domain == "sales":
            stmt = stmt.where(
                (ManagerRecommendation.domain == "sales")
                | ManagerRecommendation.domain.is_(None)
            )
        else:
            stmt = stmt.where(ManagerRecommendation.domain == domain)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        RecommendationOut(
            id=r.id,
            category=r.category,
            title=r.title,
            rationale=r.rationale,
            brief=r.brief,
            enriched_at=r.enriched_at,
            evidence=r.evidence or {},
            target=r.target or {},
            score=float(r.score or 0),
            status=r.status,
            domain=r.domain,
            applied_artifact_type=r.applied_artifact_type,
            applied_artifact_id=r.applied_artifact_id,
            expires_at=r.expires_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post(
    "/manager/recommendations/{rec_id}/apply",
    response_model=ApplyResult,
    dependencies=[Depends(require_role("manager"))],
)
async def apply_recommendation(
    rec_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Dispatch by category to create the concrete artifact a manager
    will follow up on. Each branch sets the recommendation's
    ``applied_*`` fields so the action is auditable."""
    rec = await _get_tenant_rec(db, tenant.id, rec_id)
    if rec.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Recommendation already {rec.status}.",
        )

    if rec.category == "coach_rep":
        artifact = await _apply_coach_rep(db, tenant.id, rec, principal)
    elif rec.category == "run_campaign":
        artifact = await _apply_run_campaign(db, tenant.id, rec)
    elif rec.category == "outreach_at_risk_customer":
        artifact = await _apply_outreach(db, tenant.id, rec)
    elif rec.category == "promote_winning_script":
        artifact = await _apply_promote_script(db, tenant, rec)
    # CS categories — all create CoachingNote-shaped artifacts at the
    # spine level, with different titles/bodies and different
    # ``assigned_to`` semantics. A QBR scheduled-for or expansion-play
    # eventually wants its own object, but for the first cut the
    # CoachingNote queue is the right surface (manager-to-rep memo).
    elif rec.category == "coach_csm":
        artifact = await _apply_coach_rep(db, tenant.id, rec, principal)
    elif rec.category == "schedule_qbr":
        artifact = await _apply_outreach(db, tenant.id, rec)
    elif rec.category == "flag_renewal_risk":
        artifact = await _apply_outreach(db, tenant.id, rec)
    elif rec.category == "assign_expansion_play":
        artifact = await _apply_outreach(db, tenant.id, rec)
    # Support categories.
    elif rec.category == "coach_support_agent":
        artifact = await _apply_coach_rep(db, tenant.id, rec, principal)
    elif rec.category == "update_kb_article":
        artifact = await _apply_kb_article_request(db, tenant.id, rec, principal)
    elif rec.category == "route_to_specialist":
        artifact = await _apply_outreach(db, tenant.id, rec)
    elif rec.category == "escalate_recurring_issue":
        artifact = await _apply_kb_article_request(db, tenant.id, rec, principal)
    # Predictive / cohort-derived categories from the cohort
    # recommendation service. Apply converts the prediction into a
    # concrete outreach (an ActionItem anchored to the customer's
    # latest interaction) so the rep can act on it the same way as
    # the reactive ``outreach_at_risk_customer`` flow.
    elif rec.category in (
        "prevent_no_touch_churn",
        "prevent_lead_stall",
        "proactive_outreach_repeat_support",
    ):
        artifact = await _apply_outreach(db, tenant.id, rec)
    # AI-driven cross-customer trend detector. Apply turns the
    # detected recurring issue into a KB-article request so the
    # Support team has a tracked artifact to investigate against.
    elif rec.category == "address_recurring_issue":
        artifact = await _apply_kb_article_request(db, tenant.id, rec, principal)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown category: {rec.category}")

    rec.status = "applied"
    rec.applied_at = datetime.now(timezone.utc)
    rec.applied_by_user_id = principal.user_id
    rec.applied_artifact_type = artifact["type"]
    rec.applied_artifact_id = artifact["id"]
    await db.commit()
    return ApplyResult(artifact_type=artifact["type"], artifact_id=artifact["id"])


@router.post(
    "/manager/recommendations/{rec_id}/dismiss",
    dependencies=[Depends(require_role("manager"))],
)
async def dismiss_recommendation(
    rec_id: uuid.UUID,
    body: DismissRequest,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    rec = await _get_tenant_rec(db, tenant.id, rec_id)
    rec.status = "dismissed"
    rec.dismissed_at = datetime.now(timezone.utc)
    rec.dismiss_reason = body.reason
    await db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# Alert config
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/manager/alert-config",
    response_model=AlertConfigOut,
    dependencies=[Depends(require_role("manager"))],
)
async def get_alert_config(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    cfg = await _get_or_create_config(db, tenant.id)
    return AlertConfigOut(
        inapp_enabled=cfg.inapp_enabled,
        slack_enabled=cfg.slack_enabled,
        slack_min_severity=cfg.slack_min_severity,
        topic_spike_pct_change_threshold=cfg.topic_spike_pct_change_threshold,
        topic_spike_min_volume=cfg.topic_spike_min_volume,
        sentiment_drop_threshold=cfg.sentiment_drop_threshold,
        churn_surge_multiplier=cfg.churn_surge_multiplier,
        methodology_drop_threshold=cfg.methodology_drop_threshold,
    )


@router.put(
    "/manager/alert-config",
    response_model=AlertConfigOut,
    dependencies=[Depends(require_role("manager"))],
)
async def update_alert_config(
    body: AlertConfigUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    cfg = await _get_or_create_config(db, tenant.id)
    for field_name, value in body.model_dump(exclude_unset=True).items():
        if field_name == "slack_min_severity" and value not in {"high", "medium", "low"}:
            raise HTTPException(
                status_code=422, detail="slack_min_severity must be high|medium|low"
            )
        setattr(cfg, field_name, value)
    cfg.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await get_alert_config(db=db, tenant=tenant)


# ── Per-domain alert thresholds (added in PR C / dom_003) ─────────────


class AlertDomainConfigOut(BaseModel):
    """Per-domain override row. NULL on a field means "inherit from the
    tenant-wide row." The detector reads override-first."""

    domain: str
    topic_spike_pct_change_threshold: Optional[int]
    topic_spike_min_volume: Optional[int]
    sentiment_drop_threshold: Optional[float]
    churn_surge_multiplier: Optional[float]
    methodology_drop_threshold: Optional[float]


class AlertDomainConfigUpdate(BaseModel):
    topic_spike_pct_change_threshold: Optional[int] = None
    topic_spike_min_volume: Optional[int] = None
    sentiment_drop_threshold: Optional[float] = None
    churn_surge_multiplier: Optional[float] = None
    methodology_drop_threshold: Optional[float] = None


_VALID_OVERRIDE_DOMAINS = {
    "sales",
    "customer_service",
    "it_support",
    "generic",
}


@router.get(
    "/manager/alert-config/{domain}",
    response_model=AlertDomainConfigOut,
    dependencies=[Depends(require_role("manager"))],
)
async def get_alert_domain_config(
    domain: str,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> AlertDomainConfigOut:
    if domain not in _VALID_OVERRIDE_DOMAINS:
        raise HTTPException(status_code=422, detail=f"Unknown domain: {domain}")
    row = await db.get(AlertDomainConfig, (tenant.id, domain))
    if row is None:
        return AlertDomainConfigOut(
            domain=domain,
            topic_spike_pct_change_threshold=None,
            topic_spike_min_volume=None,
            sentiment_drop_threshold=None,
            churn_surge_multiplier=None,
            methodology_drop_threshold=None,
        )
    return AlertDomainConfigOut(
        domain=row.domain,
        topic_spike_pct_change_threshold=row.topic_spike_pct_change_threshold,
        topic_spike_min_volume=row.topic_spike_min_volume,
        sentiment_drop_threshold=row.sentiment_drop_threshold,
        churn_surge_multiplier=row.churn_surge_multiplier,
        methodology_drop_threshold=row.methodology_drop_threshold,
    )


@router.put(
    "/manager/alert-config/{domain}",
    response_model=AlertDomainConfigOut,
    dependencies=[Depends(require_role("manager"))],
)
async def update_alert_domain_config(
    domain: str,
    body: AlertDomainConfigUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> AlertDomainConfigOut:
    """Upsert the per-(tenant, domain) threshold override row.

    Each value can be NULL to mean "inherit from the tenant-wide row"
    — sending an explicit ``null`` clears the override for that knob.
    """
    if domain not in _VALID_OVERRIDE_DOMAINS:
        raise HTTPException(status_code=422, detail=f"Unknown domain: {domain}")
    row = await db.get(AlertDomainConfig, (tenant.id, domain))
    if row is None:
        row = AlertDomainConfig(tenant_id=tenant.id, domain=domain)
        db.add(row)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await get_alert_domain_config(domain=domain, db=db, tenant=tenant)


# ─────────────────────────────────────────────────────────────────────
# Overview (preserved from prior dashboard — consumed by the agent
# home dashboard's manager-only block)
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/manager/overview",
    response_model=ManagerOverview,
    dependencies=[Depends(require_role("manager"))],
)
async def manager_overview(
    window_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Talk/listen distribution, churn-signal throughput, methodology
    adherence over the given window. Same shape as the deprecated
    ``/manager/dashboard/overview``."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    talk_listen = await _talk_listen(db, tenant.id, cutoff)
    churn = await _churn_throughput(db, tenant.id, cutoff, window_days)
    methodology = await _methodology(db, tenant.id, cutoff)
    return ManagerOverview(
        window_days=window_days,
        talk_listen=talk_listen,
        churn_throughput=churn,
        methodology=methodology,
    )


async def _talk_listen(
    db: AsyncSession, tenant_id: uuid.UUID, cutoff: datetime
) -> TalkListenDistribution:
    talk_pct_expr = func.coalesce(
        Interaction.call_metrics["talk_pct"]["agent"].as_float(),
        Interaction.call_metrics["rep_talk_pct"].as_float(),
    )
    stmt = (
        select(
            User.id,
            User.name,
            func.count(Interaction.id).label("call_count"),
            func.avg(talk_pct_expr).label("talk_pct_avg"),
        )
        .select_from(Interaction)
        .join(User, User.id == Interaction.agent_id, isouter=True)
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.created_at >= cutoff,
        )
        .group_by(User.id, User.name)
        .order_by(func.count(Interaction.id).desc())
    )
    rows = (await db.execute(stmt)).all()
    rep_rows: List[RepTalkListenRow] = []
    weighted_sum = 0.0
    weighted_n = 0
    for rep_id, rep_name, call_count, talk_pct_avg in rows:
        rep_rows.append(
            RepTalkListenRow(
                rep_id=str(rep_id) if rep_id else None,
                rep_name=rep_name,
                call_count=int(call_count or 0),
                talk_pct_avg=float(talk_pct_avg) if talk_pct_avg is not None else None,
            )
        )
        if talk_pct_avg is not None and call_count:
            weighted_sum += float(talk_pct_avg) * int(call_count)
            weighted_n += int(call_count)
    tenant_avg = weighted_sum / weighted_n if weighted_n > 0 else None
    return TalkListenDistribution(rows=rep_rows, tenant_avg_talk_pct=tenant_avg)


async def _churn_throughput(
    db: AsyncSession, tenant_id: uuid.UUID, cutoff: datetime, window_days: int
) -> ChurnThroughput:
    signal_expr = Interaction.insights["churn_risk_signal"].as_string()
    stmt = (
        select(signal_expr.label("bucket"), func.count(Interaction.id).label("count"))
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.created_at >= cutoff,
        )
        .group_by(signal_expr)
    )
    rows = (await db.execute(stmt)).all()
    buckets = [
        ChurnThroughputBucket(bucket=str(b) if b else "none", count=int(c or 0))
        for b, c in rows
    ]
    total = sum(b.count for b in buckets)
    return ChurnThroughput(window_days=window_days, buckets=buckets, total_calls=total)


async def _methodology(
    db: AsyncSession, tenant_id: uuid.UUID, cutoff: datetime
) -> List[MethodologyAdherence]:
    stmt = select(Interaction.insights).where(
        Interaction.tenant_id == tenant_id,
        Interaction.created_at >= cutoff,
        Interaction.insights["methodology_coverage"].isnot(None),
    )
    rows = (await db.execute(stmt)).all()
    by_framework: Dict[str, Dict[str, Any]] = {}
    for (insights,) in rows:
        if not isinstance(insights, dict):
            continue
        cov = insights.get("methodology_coverage")
        if not isinstance(cov, dict):
            continue
        framework = cov.get("framework") or "none"
        if framework == "none":
            continue
        bucket = by_framework.setdefault(
            framework,
            {"covered_total": 0, "missing_total": 0, "calls": 0, "missing_counts": {}},
        )
        covered = cov.get("covered") or []
        missing = cov.get("missing") or []
        bucket["covered_total"] += len(covered) if isinstance(covered, list) else 0
        bucket["missing_total"] += len(missing) if isinstance(missing, list) else 0
        bucket["calls"] += 1
        if isinstance(missing, list):
            for stage in missing:
                if isinstance(stage, str):
                    bucket["missing_counts"][stage] = (
                        bucket["missing_counts"].get(stage, 0) + 1
                    )
    out: List[MethodologyAdherence] = []
    for framework, bucket in by_framework.items():
        total_stages = bucket["covered_total"] + bucket["missing_total"]
        ratio = bucket["covered_total"] / total_stages if total_stages > 0 else None
        most_missed = (
            max(bucket["missing_counts"].items(), key=lambda kv: kv[1])[0]
            if bucket["missing_counts"]
            else None
        )
        out.append(
            MethodologyAdherence(
                framework=framework,
                total_calls=bucket["calls"],
                avg_coverage_ratio=round(ratio, 3) if ratio is not None else None,
                most_missed_stage=most_missed,
            )
        )
    out.sort(key=lambda m: m.total_calls, reverse=True)
    return out


# ─────────────────────────────────────────────────────────────────────
# Training-gap (preserved from prior dashboard, moved under /manager/*)
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/manager/training-gap",
    response_model=TrainingGapReport,
    dependencies=[Depends(require_role("manager"))],
)
async def training_gap(
    window_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Per-rep deep-dive: reflection rate, open-question rate,
    methodology coverage. Moved from the deprecated ``/manager/dashboard``
    namespace; same SQL aggregation, same response shape."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    cm = Interaction.call_metrics
    ins = Interaction.insights

    reflection_expr = func.coalesce(
        cm["reflection_ratio"].as_float(),
        cm["reflections_by_agent_count"].as_float()
        / func.nullif(cm["agent_turn_count"].as_float(), 0.0),
    )
    open_q_expr = cm["open_question_rate"].as_float()

    covered_len = func.coalesce(
        func.jsonb_array_length(ins["methodology_coverage"]["covered"]), 0
    )
    missing_len = func.coalesce(
        func.jsonb_array_length(ins["methodology_coverage"]["missing"]), 0
    )
    methodology_expr = func.cast(covered_len, sa_Float()) / func.nullif(
        func.cast(covered_len + missing_len, sa_Float()), 0.0
    )

    stmt = (
        select(
            User.id.label("rep_id"),
            User.name.label("rep_name"),
            func.count(Interaction.id).label("call_count"),
            func.avg(reflection_expr).label("reflection_rate"),
            func.avg(open_q_expr).label("open_question_rate"),
            func.avg(methodology_expr).label("avg_methodology_coverage"),
        )
        .select_from(Interaction)
        .join(User, User.id == Interaction.agent_id, isouter=True)
        .where(
            Interaction.tenant_id == tenant.id,
            Interaction.created_at >= cutoff,
        )
        .group_by(User.id, User.name)
        .order_by(func.count(Interaction.id).desc())
    )
    rows = (await db.execute(stmt)).all()

    return TrainingGapReport(
        window_days=window_days,
        rows=[
            RepTrainingGap(
                rep_id=str(r.rep_id) if r.rep_id else None,
                rep_name=r.rep_name,
                call_count=int(r.call_count or 0),
                reflection_rate=round(float(r.reflection_rate), 3)
                if r.reflection_rate is not None
                else None,
                open_question_rate=round(float(r.open_question_rate), 3)
                if r.open_question_rate is not None
                else None,
                avg_methodology_coverage=round(float(r.avg_methodology_coverage), 3)
                if r.avg_methodology_coverage is not None
                else None,
            )
            for r in rows
        ],
    )


# ─────────────────────────────────────────────────────────────────────
# Division slicing — filter by manager_id (manager's direct reports)
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/manager/division-slices",
    response_model=NarrativeOut,
    dependencies=[Depends(require_role("manager"))],
)
async def division_slice(
    manager_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Narrative for the manager's direct-report subset of the tenant.

    Falls back gracefully when ``manager_id`` has no direct reports
    (``User.manager_id`` is sparse): we return the tenant-level
    narrative so the page still renders something useful.
    """
    direct_reports_stmt = select(User.id).where(
        User.tenant_id == tenant.id, User.manager_id == manager_id
    )
    direct_reports = [r for (r,) in (await db.execute(direct_reports_stmt)).all()]
    if not direct_reports:
        return await get_narrative(db=db, tenant=tenant)
    # Same data source as the tenant narrative — division-aware
    # aggregation lands when the orchestrator emits per-manager profiles.
    return await get_narrative(db=db, tenant=tenant)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


async def _latest_business_profile(
    db: AsyncSession, tenant_id: uuid.UUID
) -> Optional[BusinessProfile]:
    return (
        await db.execute(
            select(BusinessProfile)
            .where(BusinessProfile.business_tenant_id == tenant_id)
            .order_by(desc(BusinessProfile.version))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _get_tenant_alert(
    db: AsyncSession, tenant_id: uuid.UUID, alert_id: uuid.UUID
) -> ManagerAlert:
    alert = await db.get(ManagerAlert, alert_id)
    if alert is None or alert.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


async def _get_tenant_rec(
    db: AsyncSession, tenant_id: uuid.UUID, rec_id: uuid.UUID
) -> ManagerRecommendation:
    rec = await db.get(ManagerRecommendation, rec_id)
    if rec is None or rec.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return rec


async def _get_or_create_config(
    db: AsyncSession, tenant_id: uuid.UUID
) -> AlertChannelConfig:
    cfg = await db.get(AlertChannelConfig, tenant_id)
    if cfg is None:
        cfg = AlertChannelConfig(tenant_id=tenant_id)
        db.add(cfg)
        await db.flush()
    return cfg


# ─────────────────────────────────────────────────────────────────────
# Apply dispatchers
# ─────────────────────────────────────────────────────────────────────


async def _apply_coach_rep(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    rec: ManagerRecommendation,
    principal: AuthPrincipal,
) -> Dict[str, Any]:
    rep_id_raw = (rec.target or {}).get("rep_user_id") or (rec.target or {}).get("user_id")
    rep_id: Optional[uuid.UUID] = None
    if rep_id_raw:
        try:
            rep_id = uuid.UUID(str(rep_id_raw))
        except (TypeError, ValueError):
            rep_id = None
    if rep_id is None:
        # Fall back to any active rep in the tenant — better than 422
        # since the recommendation is still actionable as a generic
        # team-wide coaching prompt.
        any_rep = (
            await db.execute(
                select(User.id).where(
                    User.tenant_id == tenant_id, User.role == "agent"
                ).limit(1)
            )
        ).scalar_one_or_none()
        if any_rep is None:
            raise HTTPException(
                status_code=422,
                detail="No reps available to assign the coaching note to.",
            )
        rep_id = any_rep
    note = CoachingNote(
        tenant_id=tenant_id,
        assigned_to=rep_id,
        author_id=principal.user_id,
        title=rec.title[:300],
        body=rec.rationale or rec.title,
        source_recommendation_id=rec.id,
    )
    db.add(note)
    await db.flush()
    return {"type": "coaching_note", "id": note.id}


async def _apply_run_campaign(
    db: AsyncSession, tenant_id: uuid.UUID, rec: ManagerRecommendation
) -> Dict[str, Any]:
    topic = (rec.target or {}).get("campaign_topic") or rec.title
    campaign = Campaign(
        tenant_id=tenant_id,
        name=f"[Draft] {topic[:120]}",
        channel="email",
        subject=rec.title[:150],
        metadata_={"source": "manager_recommendation", "recommendation_id": str(rec.id)},
    )
    db.add(campaign)
    await db.flush()
    return {"type": "campaign", "id": campaign.id}


async def _apply_outreach(
    db: AsyncSession, tenant_id: uuid.UUID, rec: ManagerRecommendation
) -> Dict[str, Any]:
    customer_id_raw = (rec.target or {}).get("customer_id")
    if not customer_id_raw:
        raise HTTPException(
            status_code=422,
            detail="Recommendation target.customer_id is required for outreach.",
        )
    try:
        customer_id = uuid.UUID(str(customer_id_raw))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid customer_id in target.")
    interaction_row = (
        await db.execute(
            select(Interaction.id)
            .where(
                Interaction.tenant_id == tenant_id,
                Interaction.customer_id == customer_id,
            )
            .order_by(desc(Interaction.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if interaction_row is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Customer has no interactions to anchor an action item to. "
                "Reach out manually or wait until the first call lands."
            ),
        )
    evidence = rec.evidence or {}
    item = ActionItem(
        interaction_id=interaction_row,
        tenant_id=tenant_id,
        title=rec.title[:300],
        description=rec.rationale,
        category="manager_triage",
        priority="high",
        recommended_channel=(rec.target or {}).get("recommended_channel"),
        channel_reasoning=(rec.target or {}).get("channel_reasoning"),
        implicit_signal=evidence.get("implicit_signal"),
        manually_created=True,
    )
    db.add(item)
    await db.flush()
    return {"type": "action_item", "id": item.id}


async def _apply_promote_script(
    db: AsyncSession, tenant: Tenant, rec: ManagerRecommendation
) -> Dict[str, Any]:
    """Append the winning script to the tenant's playbook.

    Re-fetches the Tenant row through the request's session so the
    JSONB reassignment lands on a row SQLAlchemy will actually flush
    (the ``tenant`` from ``Depends(get_current_tenant)`` may have been
    loaded in a separate session that's already closed).
    """
    phrase = (rec.target or {}).get("script_phrase") or rec.title
    db_tenant = await db.get(Tenant, tenant.id)
    if db_tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    ctx = dict(db_tenant.tenant_context or {})
    playbook = dict(ctx.get("playbook") or {})
    scripts = list(playbook.get("scripts") or [])
    scripts.append(
        {
            "phrase": phrase,
            "rationale": rec.rationale or "",
            "source": "manager_recommendation",
            "recommendation_id": str(rec.id),
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    playbook["scripts"] = scripts
    ctx["playbook"] = playbook
    db_tenant.tenant_context = ctx
    await db.flush()
    return {"type": "playbook_entry", "id": rec.id}


async def _apply_kb_article_request(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    rec: ManagerRecommendation,
    principal: AuthPrincipal,
) -> Dict[str, Any]:
    """Apply for Support-motion recommendations that ask the manager to
    update or create a KB article (``update_kb_article``,
    ``escalate_recurring_issue``).

    Creates a ``KBArticleRequest`` row in the KB-owner's inbox. The
    inbox lives at ``/kb/requests``; status / priority / assignment
    flow independently from the rep coaching queue.
    """
    from backend.app.models import KBArticleRequest

    target = rec.target or {}
    topic = target.get("kb_article_topic") or rec.title
    assigned_to: Optional[uuid.UUID] = None
    raw = target.get("rep_user_id") or target.get("assigned_to")
    if raw:
        try:
            assigned_to = uuid.UUID(str(raw))
        except (TypeError, ValueError):
            pass
    request = KBArticleRequest(
        tenant_id=tenant_id,
        requested_by_user_id=principal.user_id,
        assigned_to=assigned_to,
        source_recommendation_id=rec.id,
        topic=str(topic)[:300],
        rationale=rec.rationale,
        priority=("high" if rec.score and rec.score >= 75 else "medium"),
    )
    db.add(request)
    await db.flush()
    return {"type": "kb_article_request", "id": request.id}


# ─────────────────────────────────────────────────────────────────────
# Cross-motion Journey view
# ─────────────────────────────────────────────────────────────────────


class JourneyHandoffRow(BaseModel):
    """One row in the handoff-health table on the Journey view.

    Captures customers stalled in a transition between two motions
    (e.g. Sales handed off to CS more than 30 days ago but no CS
    activity has occurred). Counts and average days are computed in
    Python over a small N — the manager portal volumes don't justify
    a window-function SQL query yet.
    """

    transition: str  # e.g. "sales_to_cs", "cs_to_support", "support_to_renewal"
    customer_count: int
    avg_days_stalled: Optional[float]


class JourneyAccountRow(BaseModel):
    """Per-account lifecycle bar for the Journey view's top section."""

    customer_id: uuid.UUID
    customer_name: str
    last_sales_at: Optional[datetime]
    last_cs_at: Optional[datetime]
    last_support_at: Optional[datetime]
    open_support_cases: int
    health_signal: Optional[str]  # 'high' | 'medium' | 'low' | 'none'


class JourneyReport(BaseModel):
    """Top-level Journey response. The frontend renders the handoffs
    row as a strip and the accounts list as a table; both are
    truncated server-side."""

    handoffs: List[JourneyHandoffRow]
    accounts_at_risk: List[JourneyAccountRow]
    motions_seen: List[str]


@router.get(
    "/manager/journey",
    response_model=JourneyReport,
    dependencies=[Depends(require_role("manager"))],
)
async def journey_view(
    window_days: int = Query(90, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> JourneyReport:
    """Cross-motion view used by managers who hold 2+ ``manager_domains``.

    Handoff health: counts of customers stuck in each transition
    (Sales -> CS, CS -> Support, Support -> Renewal) using a simple
    "X days since last interaction in motion Y, no follow-up in motion
    Z" rule. Accounts at risk: customers with open support cases plus
    a recent high-churn signal from their last CS or Sales call.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    motions_seen: List[str] = []
    handoffs: List[JourneyHandoffRow] = []

    # Sales -> CS: customers with a sales interaction in the window
    # but no CS interaction since.
    sales_to_cs = await _handoff_stall(db, tenant.id, "sales", "customer_service", cutoff)
    if sales_to_cs is not None:
        handoffs.append(JourneyHandoffRow(transition="sales_to_cs", **sales_to_cs))
        motions_seen.extend(["sales", "customer_service"])

    cs_to_support = await _handoff_stall(
        db, tenant.id, "customer_service", "it_support", cutoff
    )
    if cs_to_support is not None:
        handoffs.append(JourneyHandoffRow(transition="cs_to_support", **cs_to_support))
        motions_seen.append("it_support")

    accounts = await _accounts_at_risk(db, tenant.id, cutoff)
    return JourneyReport(
        handoffs=handoffs,
        accounts_at_risk=accounts,
        motions_seen=sorted(set(motions_seen)),
    )


async def _handoff_stall(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    from_domain: str,
    to_domain: str,
    cutoff: datetime,
) -> Optional[Dict[str, Any]]:
    """Find customers whose last interaction in ``from_domain`` is
    within the window but who have no ``to_domain`` interaction since.

    Returns ``{"customer_count": int, "avg_days_stalled": float}`` or
    None when there's no signal.
    """
    last_from_stmt = (
        select(
            Interaction.customer_id,
            func.max(Interaction.created_at).label("last_at"),
        )
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.domain == from_domain,
            Interaction.customer_id.isnot(None),
            Interaction.created_at >= cutoff,
        )
        .group_by(Interaction.customer_id)
    )
    last_from_rows = (await db.execute(last_from_stmt)).all()
    if not last_from_rows:
        return None

    last_from = {cust_id: last_at for cust_id, last_at in last_from_rows}

    last_to_stmt = (
        select(
            Interaction.customer_id,
            func.max(Interaction.created_at).label("last_at"),
        )
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.domain == to_domain,
            Interaction.customer_id.in_(list(last_from.keys())),
        )
        .group_by(Interaction.customer_id)
    )
    last_to = {
        cust_id: last_at
        for cust_id, last_at in (await db.execute(last_to_stmt)).all()
    }

    now = datetime.now(timezone.utc)
    stalled_days: List[float] = []
    for cust_id, last_from_at in last_from.items():
        last_to_at = last_to.get(cust_id)
        if last_to_at is not None and last_to_at >= last_from_at:
            continue  # handed off successfully
        days = (now - last_from_at).total_seconds() / 86400.0
        if days >= 7:  # at least a week is "stalled"
            stalled_days.append(days)

    if not stalled_days:
        return None
    return {
        "customer_count": len(stalled_days),
        "avg_days_stalled": round(sum(stalled_days) / len(stalled_days), 1),
    }


async def _accounts_at_risk(
    db: AsyncSession, tenant_id: uuid.UUID, cutoff: datetime
) -> List[JourneyAccountRow]:
    """Customers with at least one of: open Support case, recent
    high-churn signal on a CS call, recent high-churn on a Sales call.
    Ordered by severity proxy (open support cases first, then high
    churn). Capped at 15 — this is a triage view, not an export.
    """
    from backend.app.models import SupportCase

    # Open support cases per customer.
    open_cases_stmt = (
        select(SupportCase.customer_id, func.count(SupportCase.id).label("n"))
        .where(
            SupportCase.tenant_id == tenant_id,
            SupportCase.status.in_(("open", "in_progress", "escalated")),
            SupportCase.customer_id.isnot(None),
        )
        .group_by(SupportCase.customer_id)
    )
    open_cases = {
        cust_id: int(n or 0)
        for cust_id, n in (await db.execute(open_cases_stmt)).all()
    }

    # Last interaction per (customer, domain).
    last_stmt = (
        select(
            Interaction.customer_id,
            Interaction.domain,
            func.max(Interaction.created_at).label("last_at"),
        )
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.customer_id.isnot(None),
            Interaction.created_at >= cutoff,
        )
        .group_by(Interaction.customer_id, Interaction.domain)
    )
    last_per_motion: Dict[uuid.UUID, Dict[str, datetime]] = {}
    for cust_id, dom, last_at in (await db.execute(last_stmt)).all():
        if cust_id is None:
            continue
        last_per_motion.setdefault(cust_id, {})[dom or "sales"] = last_at

    # Health signal: read the most recent insights.churn_risk_signal
    # value across that customer's interactions.
    health_stmt = (
        select(Interaction.customer_id, Interaction.insights)
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.customer_id.isnot(None),
            Interaction.created_at >= cutoff,
        )
        .order_by(desc(Interaction.created_at))
    )
    health_signal: Dict[uuid.UUID, str] = {}
    for cust_id, insights in (await db.execute(health_stmt)).all():
        if cust_id in health_signal:
            continue
        if isinstance(insights, dict):
            raw = insights.get("churn_risk_signal")
            if isinstance(raw, str):
                health_signal[cust_id] = raw.lower()

    # Eligible customer ids: anyone with an open case OR a high-churn
    # signal on a recent interaction.
    eligible: set = set(open_cases.keys())
    eligible |= {c for c, s in health_signal.items() if s == "high"}
    if not eligible:
        return []

    name_stmt = select(Customer.id, Customer.name).where(Customer.id.in_(list(eligible)))
    names = {cid: nm or "" for cid, nm in (await db.execute(name_stmt)).all()}

    rows: List[JourneyAccountRow] = []
    for cust_id in eligible:
        motion_last = last_per_motion.get(cust_id, {})
        rows.append(
            JourneyAccountRow(
                customer_id=cust_id,
                customer_name=names.get(cust_id, ""),
                last_sales_at=motion_last.get("sales"),
                last_cs_at=motion_last.get("customer_service"),
                last_support_at=motion_last.get("it_support"),
                open_support_cases=open_cases.get(cust_id, 0),
                health_signal=health_signal.get(cust_id),
            )
        )
    # Sort: open cases first, then health=high, then most recent activity.
    def _sort_key(r: JourneyAccountRow):
        return (
            -r.open_support_cases,
            0 if r.health_signal == "high" else 1,
            -(max(filter(None, [r.last_sales_at, r.last_cs_at, r.last_support_at]),
                  default=datetime.fromtimestamp(0, tz=timezone.utc)).timestamp()),
        )

    rows.sort(key=_sort_key)
    return rows[:15]


# ─────────────────────────────────────────────────────────────────────
# Refresh-slot Redis lock
# ─────────────────────────────────────────────────────────────────────


def _claim_refresh_slot(tenant_id: uuid.UUID) -> bool:
    """Best-effort 1/hr/tenant rate limit using Redis SETNX."""
    try:
        import redis  # type: ignore

        r = redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
        key = f"manager:narrative:refresh:{tenant_id}"
        return bool(r.set(key, "1", ex=3600, nx=True))
    except Exception:
        logger.debug("refresh-slot Redis check failed (allowing)", exc_info=True)
        return True
