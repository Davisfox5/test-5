"""Manager dashboard — aggregates across the tenant's reps + calls.

Endpoints are manager/admin-gated; agents don't see other reps' data.
The aggregations are cheap window queries (last 30 days by default)
to keep the dashboard snappy without a precomputed cache. We can
move to a materialized view later if a tenant gets noisy enough.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant, require_role
from backend.app.db import get_db
from backend.app.models import Interaction, Tenant, User

logger = logging.getLogger(__name__)

router = APIRouter()


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
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    talk_listen = await _talk_listen_distribution(db, tenant.id, cutoff)
    churn = await _churn_throughput(db, tenant.id, cutoff, window_days)
    methodology = await _methodology_adherence(db, tenant.id, cutoff)

    return DashboardOverview(
        window_days=window_days,
        talk_listen=talk_listen,
        churn_throughput=churn,
        methodology=methodology,
    )


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
    each other's diagnostics."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    stmt = (
        select(
            User.id,
            User.name,
            Interaction.call_metrics,
            Interaction.insights,
        )
        .select_from(Interaction)
        .join(User, User.id == Interaction.agent_id, isouter=True)
        .where(
            Interaction.tenant_id == tenant.id,
            Interaction.created_at >= cutoff,
        )
    )
    rows = (await db.execute(stmt)).all()

    by_rep: Dict[str, Dict] = {}
    for rep_id, rep_name, call_metrics, insights in rows:
        key = str(rep_id) if rep_id else "_unassigned"
        bucket = by_rep.setdefault(
            key,
            {
                "rep_id": str(rep_id) if rep_id else None,
                "rep_name": rep_name,
                "call_count": 0,
                "reflection_rate_sum": 0.0,
                "reflection_rate_n": 0,
                "open_q_rate_sum": 0.0,
                "open_q_rate_n": 0,
                "methodology_sum": 0.0,
                "methodology_n": 0,
            },
        )
        bucket["call_count"] += 1

        # Reflection rate — call_metrics may carry it under a few names
        # depending on the seed; tolerate both shapes.
        cm = call_metrics if isinstance(call_metrics, dict) else {}
        rr = cm.get("reflection_ratio")
        if rr is None:
            ref_count = cm.get("reflections_by_agent_count")
            agent_turns = cm.get("agent_turn_count")
            if (
                isinstance(ref_count, (int, float))
                and isinstance(agent_turns, (int, float))
                and agent_turns > 0
            ):
                rr = ref_count / agent_turns
        if isinstance(rr, (int, float)):
            bucket["reflection_rate_sum"] += float(rr)
            bucket["reflection_rate_n"] += 1

        # Open-question rate.
        oq = cm.get("open_question_rate")
        if isinstance(oq, (int, float)):
            bucket["open_q_rate_sum"] += float(oq)
            bucket["open_q_rate_n"] += 1

        # Methodology coverage ratio.
        ins = insights if isinstance(insights, dict) else {}
        cov = ins.get("methodology_coverage")
        if isinstance(cov, dict):
            covered = cov.get("covered") or []
            missing = cov.get("missing") or []
            total = len(covered) + len(missing) if isinstance(covered, list) else 0
            if total > 0:
                ratio = len(covered) / total
                bucket["methodology_sum"] += ratio
                bucket["methodology_n"] += 1

    out: List[RepTrainingGap] = []
    for bucket in by_rep.values():
        out.append(
            RepTrainingGap(
                rep_id=bucket["rep_id"],
                rep_name=bucket["rep_name"],
                call_count=bucket["call_count"],
                reflection_rate=(
                    round(
                        bucket["reflection_rate_sum"]
                        / bucket["reflection_rate_n"],
                        3,
                    )
                    if bucket["reflection_rate_n"] > 0
                    else None
                ),
                open_question_rate=(
                    round(
                        bucket["open_q_rate_sum"] / bucket["open_q_rate_n"],
                        3,
                    )
                    if bucket["open_q_rate_n"] > 0
                    else None
                ),
                avg_methodology_coverage=(
                    round(
                        bucket["methodology_sum"] / bucket["methodology_n"],
                        3,
                    )
                    if bucket["methodology_n"] > 0
                    else None
                ),
            )
        )
    out.sort(key=lambda r: r.call_count, reverse=True)
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
