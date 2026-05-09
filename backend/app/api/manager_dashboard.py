"""Manager dashboard — aggregates across the tenant's reps + calls.

Endpoints are manager/admin-gated; agents don't see other reps' data.
The aggregations are cheap window queries (last 30 days by default)
to keep the dashboard snappy without a precomputed cache. We can
move to a materialized view later if a tenant gets noisy enough.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Float as sa_Float
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant, require_role
from backend.app.config import get_settings
from backend.app.db import get_db
from backend.app.models import Interaction, Tenant, User

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Response cache (best-effort Redis) ───────────────────────────────────

_DASHBOARD_CACHE_TTL_SECONDS = 60
_DASHBOARD_CACHE_KEY = "dash:overview:v1:{tenant_id}:{window_days}"


def _dashboard_redis():
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception:  # pragma: no cover
        return None


# ── Output shapes ───────────────────────────────────────────────────────


class RepTalkListenRow(BaseModel):
    rep_id: Optional[str]
    rep_name: Optional[str]
    call_count: int
    talk_pct_avg: Optional[float]


class TalkListenDistribution(BaseModel):
    rows: List[RepTalkListenRow]
    tenant_avg_talk_pct: Optional[float]


class ChurnThroughputBucket(BaseModel):
    bucket: str  # 'high' | 'medium' | 'low' | 'none'
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


class DashboardOverview(BaseModel):
    window_days: int
    talk_listen: TalkListenDistribution
    churn_throughput: ChurnThroughput
    methodology: List[MethodologyAdherence]


class RepTrainingGap(BaseModel):
    """Per-rep deep-dive metrics for the training-gap surface."""

    rep_id: Optional[str]
    rep_name: Optional[str]
    call_count: int
    reflection_rate: Optional[float]  # 0..1 — fraction of agent turns that reflected
    open_question_rate: Optional[float]  # 0..1 — fraction of agent questions that were open
    avg_methodology_coverage: Optional[float]  # 0..1


class TrainingGapReport(BaseModel):
    window_days: int
    rows: List[RepTrainingGap]


# ── Endpoint ────────────────────────────────────────────────────────────


@router.get(
    "/manager/dashboard/overview",
    response_model=DashboardOverview,
    dependencies=[Depends(require_role("manager"))],
)
async def manager_dashboard_overview(
    window_days: int = Query(30, ge=1, le=365),
    nocache: int = Query(0, ge=0, le=1, description="Bypass Redis cache (QA tool)."),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Three aggregations in one call:

    1. Talk/listen distribution per rep — avg talk_pct + call count
       over the window, plus the tenant-wide average.
    2. Churn throughput — how many calls landed in each
       churn_risk_signal bucket over the window.
    3. Methodology adherence — per-framework, avg coverage ratio
       (covered / (covered + missing)) and the stage that was missed
       most often.

    Result is cached in Redis for 60s (per tenant + window). Pass
    ``?nocache=1`` to skip the cache layer for QA.
    """
    cache_key = _DASHBOARD_CACHE_KEY.format(
        tenant_id=tenant.id, window_days=window_days
    )
    r = _dashboard_redis() if not nocache else None
    if r is not None:
        try:
            blob = r.get(cache_key)
            if blob:
                return DashboardOverview.model_validate_json(blob)
        except Exception:  # pragma: no cover
            logger.debug("dashboard cache get failed", exc_info=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    talk_listen = await _talk_listen_distribution(db, tenant.id, cutoff)
    churn = await _churn_throughput(db, tenant.id, cutoff, window_days)
    methodology = await _methodology_adherence(db, tenant.id, cutoff)

    response = DashboardOverview(
        window_days=window_days,
        talk_listen=talk_listen,
        churn_throughput=churn,
        methodology=methodology,
    )

    if r is not None:
        try:
            r.setex(
                cache_key,
                _DASHBOARD_CACHE_TTL_SECONDS,
                response.model_dump_json(),
            )
        except Exception:  # pragma: no cover
            logger.debug("dashboard cache set failed", exc_info=True)

    return response


@router.get(
    "/manager/dashboard/training-gap",
    response_model=TrainingGapReport,
    dependencies=[Depends(require_role("manager"))],
)
async def manager_dashboard_training_gap(
    window_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Per-rep training-gap deep-dive: reflection rate, open-question
    rate, methodology coverage. The metrics live on
    ``Interaction.call_metrics`` (deterministic) and
    ``insights.methodology_coverage`` (LLM-extracted). Per the Phase 5
    spec, these surface only for managers + admins — agents don't see
    each other's diagnostics.

    The aggregation runs in SQL — Postgres extracts the JSONB scalars
    and aggregates per-rep, so the wire payload is small (a few hundred
    bytes per rep instead of full call_metrics + insights blobs).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    cm = Interaction.call_metrics
    ins = Interaction.insights

    # Prefer call_metrics->>'reflection_ratio'; fall back to the
    # ``reflections_by_agent_count / agent_turn_count`` ratio that older
    # seeds populate. NULLIF guards the division.
    reflection_expr = func.coalesce(
        cm["reflection_ratio"].as_float(),
        cm["reflections_by_agent_count"].as_float()
        / func.nullif(cm["agent_turn_count"].as_float(), 0.0),
    )
    open_q_expr = cm["open_question_rate"].as_float()

    # Methodology coverage = covered / (covered + missing). We expose
    # both array lengths and let Postgres do the division per-row.
    covered_len = func.coalesce(
        func.jsonb_array_length(ins["methodology_coverage"]["covered"]),
        0,
    )
    missing_len = func.coalesce(
        func.jsonb_array_length(ins["methodology_coverage"]["missing"]),
        0,
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

    out: List[RepTrainingGap] = [
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
    ]
    return TrainingGapReport(window_days=window_days, rows=out)


# ── Aggregation helpers ─────────────────────────────────────────────────


async def _talk_listen_distribution(
    db: AsyncSession, tenant_id, cutoff: datetime
) -> TalkListenDistribution:
    """Per-rep avg talk_pct from call_metrics over the window.

    Pulls call_metrics->'talk_pct'->'agent' as a JSON path. Falls back
    to top-level call_metrics->'rep_talk_pct' when the nested shape
    isn't populated (older interactions).
    """
    # Build the JSON-path expression once; SQLAlchemy + JSONB lets us
    # operate on either shape with a coalesce.
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
                talk_pct_avg=(
                    float(talk_pct_avg) if talk_pct_avg is not None else None
                ),
            )
        )
        if talk_pct_avg is not None and call_count:
            weighted_sum += float(talk_pct_avg) * int(call_count)
            weighted_n += int(call_count)

    tenant_avg = weighted_sum / weighted_n if weighted_n > 0 else None
    return TalkListenDistribution(
        rows=rep_rows,
        tenant_avg_talk_pct=tenant_avg,
    )


async def _churn_throughput(
    db: AsyncSession, tenant_id, cutoff: datetime, window_days: int
) -> ChurnThroughput:
    """Count interactions by churn_risk_signal bucket over the window."""
    signal_expr = Interaction.insights["churn_risk_signal"].as_string()
    stmt = (
        select(
            signal_expr.label("bucket"),
            func.count(Interaction.id).label("count"),
        )
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.created_at >= cutoff,
        )
        .group_by(signal_expr)
    )
    rows = (await db.execute(stmt)).all()
    buckets = [
        ChurnThroughputBucket(
            bucket=str(b) if b else "none",
            count=int(c or 0),
        )
        for b, c in rows
    ]
    total = sum(b.count for b in buckets)
    return ChurnThroughput(
        window_days=window_days,
        buckets=buckets,
        total_calls=total,
    )


async def _methodology_adherence(
    db: AsyncSession, tenant_id, cutoff: datetime
) -> List[MethodologyAdherence]:
    """Per-framework coverage ratio + most-missed stage.

    Methodology coverage lives on each interaction's insights JSON
    under ``methodology_coverage`` (keys: framework, covered, missing,
    next_question). We pull the relevant rows in one query and reduce
    in Python — the data is per-interaction so we can't easily
    compute the ratio in SQL without unnesting JSONB arrays.
    """
    stmt = (
        select(Interaction.insights)
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.created_at >= cutoff,
            Interaction.insights["methodology_coverage"].isnot(None),
        )
    )
    rows = (await db.execute(stmt)).all()

    by_framework: Dict[str, Dict] = {}
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
        ratio = (
            bucket["covered_total"] / total_stages if total_stages > 0 else None
        )
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
