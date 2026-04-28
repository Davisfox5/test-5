"""Analytics API — aggregated metrics, trends, and team performance.

All sentiment aggregations read ``insights->>'sentiment_score'`` (numeric
0–10 from ``AIAnalysisService``).  Categorical ``sentiment_overall`` is only
used for distribution counts.  Churn and upsell are stored both as
categorical signals (``churn_risk_signal`` / ``upsell_signal``) and as
numeric scores (``churn_risk`` / ``upsell_score``) — numeric fields are
used for averages and thresholding, categorical for bucket counts.
"""

import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Tenant
from backend.app.plans import require_active_subscription

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class ChannelBreakdown(BaseModel):
    channel: str
    count: int
    avg_sentiment: Optional[float] = None


class TopTopic(BaseModel):
    name: str
    mentions: int
    avg_relevance: Optional[float] = None


class BusinessHealth(BaseModel):
    health_score: float
    total_interactions: int
    avg_sentiment: Optional[float]
    channels_breakdown: List[ChannelBreakdown]
    top_topics: List[TopTopic]


class TrendPoint(BaseModel):
    date: str
    interaction_count: int
    avg_sentiment: Optional[float]
    channel: Optional[str]


class AgentStats(BaseModel):
    agent_id: uuid.UUID
    name: Optional[str]
    interaction_count: int
    avg_sentiment: Optional[float]
    avg_scorecard_score: Optional[float]
    churn_flags: int


class ClientTrends(BaseModel):
    contact_id: uuid.UUID
    sentiment_over_time: List[Dict]
    interaction_history: List[Dict]
    churn_risk: Optional[float]
    churn_risk_signal: Optional[str]


class CompetitorRow(BaseModel):
    competitor: str
    mentions: int
    handled_well: int
    handled_well_pct: float


class TopicTrend(BaseModel):
    name: str
    mentions: int
    avg_relevance: Optional[float]
    pct_change: Optional[float]


class ProductFeedbackTheme(BaseModel):
    theme: str
    positive_count: int
    negative_count: int
    neutral_count: int
    sample_quote: Optional[str]


class CoachingInsights(BaseModel):
    avg_script_adherence: Optional[float]
    top_compliance_gaps: List[Dict]
    top_improvements: List[Dict]
    top_strengths: List[Dict]


class SignalBuckets(BaseModel):
    churn: Dict[str, int]
    upsell: Dict[str, int]
    avg_churn_risk: Optional[float]
    avg_upsell_score: Optional[float]
    by_channel: List[Dict]


class DashboardSummary(BaseModel):
    total_interactions: int
    avg_sentiment_score: Optional[float]
    action_items_open: int
    avg_qa_score: Optional[float]
    prev_period_deltas: Dict[str, Optional[float]]


class TenantInsightRow(BaseModel):
    id: uuid.UUID
    period_start: Optional[str]
    period_end: Optional[str]
    insights: Dict
    created_at: str


# ── Interval helper ──────────────────────────────────────

_INTERVAL_MAP = {"7d": "7 days", "30d": "30 days", "90d": "90 days"}


def _interval(period: str) -> str:
    """Validate and map a period string to a Postgres INTERVAL literal."""
    if period not in _INTERVAL_MAP:
        raise HTTPException(status_code=400, detail="invalid period")
    return _INTERVAL_MAP[period]


# ── Endpoints ────────────────────────────────────────────


@router.get(
    "/analytics/business",
    response_model=BusinessHealth,
    # Gate every analytics endpoint that runs heavy aggregations behind
    # ``require_active_subscription``. Dashboard summaries (used by the
    # SPA's first-paint /dashboard) intentionally stay open so an
    # expired-trial tenant can still reach the upgrade banner.
    dependencies=[Depends(require_active_subscription)],
)
async def business_health(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Business health overview computed from the last N days of interactions."""
    tenant_id = str(tenant.id)
    interval = _interval(period)

    # Total interactions & avg sentiment
    summary_query = text(f"""
        SELECT
            COUNT(*) AS total_interactions,
            AVG((insights->>'sentiment_score')::float) AS avg_sentiment
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
    """)
    summary_row = (await db.execute(summary_query, {"tenant_id": tenant_id})).fetchone()
    total_interactions = summary_row[0] if summary_row else 0
    avg_sentiment = float(summary_row[1]) if summary_row and summary_row[1] is not None else None

    # Channel breakdown with sentiment per channel
    channel_query = text(f"""
        SELECT channel,
               COUNT(*) AS count,
               AVG((insights->>'sentiment_score')::float) AS avg_sentiment
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
        GROUP BY channel
        ORDER BY count DESC
    """)
    channel_rows = (await db.execute(channel_query, {"tenant_id": tenant_id})).fetchall()
    channels_breakdown = [
        ChannelBreakdown(
            channel=row[0],
            count=row[1],
            avg_sentiment=float(row[2]) if row[2] is not None else None,
        )
        for row in channel_rows
    ]

    # Top topics — topics are {name, relevance, mentions} objects
    topics_query = text(f"""
        SELECT topic->>'name' AS name,
               SUM(COALESCE((topic->>'mentions')::int, 1)) AS mentions,
               AVG((topic->>'relevance')::float) AS avg_relevance
        FROM interactions,
             jsonb_array_elements(insights->'topics') AS topic
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
          AND insights ? 'topics'
          AND jsonb_typeof(insights->'topics') = 'array'
          AND topic->>'name' IS NOT NULL
        GROUP BY topic->>'name'
        ORDER BY mentions DESC
        LIMIT 10
    """)
    topic_rows = (await db.execute(topics_query, {"tenant_id": tenant_id})).fetchall()
    top_topics = [
        TopTopic(
            name=row[0],
            mentions=int(row[1]),
            avg_relevance=float(row[2]) if row[2] is not None else None,
        )
        for row in topic_rows
    ]

    # Health score: sentiment_score is [0, 10] → normalize to [0, 100]
    if avg_sentiment is not None:
        health_score = round(min(100.0, max(0.0, avg_sentiment * 10)), 1)
    else:
        health_score = 50.0

    return BusinessHealth(
        health_score=health_score,
        total_interactions=total_interactions,
        avg_sentiment=avg_sentiment,
        channels_breakdown=channels_breakdown,
        top_topics=top_topics,
    )


@router.get(
    "/analytics/trends",
    response_model=List[TrendPoint],
    dependencies=[Depends(require_active_subscription)],
)
async def trends(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Time-series interaction trends grouped by date and channel."""
    tenant_id = str(tenant.id)
    interval = _interval(period)

    query = text(f"""
        SELECT
            DATE(created_at) AS date,
            channel,
            COUNT(*) AS interaction_count,
            AVG((insights->>'sentiment_score')::float) AS avg_sentiment
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
        GROUP BY DATE(created_at), channel
        ORDER BY date ASC, channel
    """)
    rows = (await db.execute(query, {"tenant_id": tenant_id})).fetchall()
    return [
        TrendPoint(
            date=str(row[0]),
            channel=row[1],
            interaction_count=row[2],
            avg_sentiment=float(row[3]) if row[3] is not None else None,
        )
        for row in rows
    ]


@router.get(
    "/analytics/team",
    response_model=List[AgentStats],
    dependencies=[Depends(require_active_subscription)],
)
async def team_stats(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Agent-level performance stats for the tenant."""
    tenant_id = str(tenant.id)

    query = text("""
        SELECT
            u.id AS agent_id,
            u.name,
            COUNT(i.id) AS interaction_count,
            AVG((i.insights->>'sentiment_score')::float) AS avg_sentiment,
            AVG(s.total_score) AS avg_scorecard_score,
            COUNT(CASE WHEN (i.insights->>'churn_risk')::float > 0.7 THEN 1 END) AS churn_flags
        FROM users u
        LEFT JOIN interactions i ON i.agent_id = u.id AND i.tenant_id = :tenant_id
        LEFT JOIN interaction_scores s ON s.interaction_id = i.id
        WHERE u.tenant_id = :tenant_id
          AND u.role = 'agent'
        GROUP BY u.id, u.name
        ORDER BY interaction_count DESC
    """)
    rows = (await db.execute(query, {"tenant_id": tenant_id})).fetchall()
    return [
        AgentStats(
            agent_id=row[0],
            name=row[1],
            interaction_count=row[2],
            avg_sentiment=float(row[3]) if row[3] is not None else None,
            avg_scorecard_score=float(row[4]) if row[4] is not None else None,
            churn_flags=row[5],
        )
        for row in rows
    ]


@router.get(
    "/analytics/agents/{agent_id}",
    response_model=AgentStats,
    dependencies=[Depends(require_active_subscription)],
)
async def agent_detail(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Detailed stats for a single agent."""
    tenant_id = str(tenant.id)

    query = text("""
        SELECT
            u.id AS agent_id,
            u.name,
            COUNT(i.id) AS interaction_count,
            AVG((i.insights->>'sentiment_score')::float) AS avg_sentiment,
            AVG(s.total_score) AS avg_scorecard_score,
            COUNT(CASE WHEN (i.insights->>'churn_risk')::float > 0.7 THEN 1 END) AS churn_flags
        FROM users u
        LEFT JOIN interactions i ON i.agent_id = u.id AND i.tenant_id = :tenant_id
        LEFT JOIN interaction_scores s ON s.interaction_id = i.id
        WHERE u.id = :agent_id
          AND u.tenant_id = :tenant_id
        GROUP BY u.id, u.name
    """)
    row = (await db.execute(query, {"tenant_id": tenant_id, "agent_id": str(agent_id)})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentStats(
        agent_id=row[0],
        name=row[1],
        interaction_count=row[2],
        avg_sentiment=float(row[3]) if row[3] is not None else None,
        avg_scorecard_score=float(row[4]) if row[4] is not None else None,
        churn_flags=row[5],
    )


@router.get(
    "/analytics/clients/{contact_id}",
    response_model=ClientTrends,
    dependencies=[Depends(require_active_subscription)],
)
async def client_trends(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Per-client sentiment trends, interaction history, and churn risk."""
    tenant_id = str(tenant.id)

    verify_query = text("""
        SELECT id FROM contacts
        WHERE id = :contact_id AND tenant_id = :tenant_id
    """)
    verify = await db.execute(verify_query, {"contact_id": str(contact_id), "tenant_id": tenant_id})
    if not verify.fetchone():
        raise HTTPException(status_code=404, detail="Contact not found")

    sentiment_query = text("""
        SELECT
            DATE(created_at) AS date,
            AVG((insights->>'sentiment_score')::float) AS avg_sentiment
        FROM interactions
        WHERE contact_id = :contact_id
          AND tenant_id = :tenant_id
        GROUP BY DATE(created_at)
        ORDER BY date ASC
    """)
    sentiment_rows = (
        await db.execute(sentiment_query, {"contact_id": str(contact_id), "tenant_id": tenant_id})
    ).fetchall()
    sentiment_over_time = [
        {"date": str(row[0]), "avg_sentiment": float(row[1]) if row[1] is not None else None}
        for row in sentiment_rows
    ]

    history_query = text("""
        SELECT id, channel, title, status, created_at
        FROM interactions
        WHERE contact_id = :contact_id
          AND tenant_id = :tenant_id
        ORDER BY created_at DESC
        LIMIT 50
    """)
    history_rows = (
        await db.execute(history_query, {"contact_id": str(contact_id), "tenant_id": tenant_id})
    ).fetchall()
    interaction_history = [
        {
            "id": str(row[0]),
            "channel": row[1],
            "title": row[2],
            "status": row[3],
            "created_at": str(row[4]),
        }
        for row in history_rows
    ]

    churn_query = text("""
        SELECT (insights->>'churn_risk')::float,
               insights->>'churn_risk_signal'
        FROM interactions
        WHERE contact_id = :contact_id
          AND tenant_id = :tenant_id
          AND (insights ? 'churn_risk' OR insights ? 'churn_risk_signal')
        ORDER BY created_at DESC
        LIMIT 1
    """)
    churn_row = (
        await db.execute(churn_query, {"contact_id": str(contact_id), "tenant_id": tenant_id})
    ).fetchone()
    churn_risk = float(churn_row[0]) if churn_row and churn_row[0] is not None else None
    churn_risk_signal = churn_row[1] if churn_row else None

    return ClientTrends(
        contact_id=contact_id,
        sentiment_over_time=sentiment_over_time,
        interaction_history=interaction_history,
        churn_risk=churn_risk,
        churn_risk_signal=churn_risk_signal,
    )


@router.get(
    "/analytics/competitive",
    response_model=List[CompetitorRow],
    dependencies=[Depends(require_active_subscription)],
)
async def competitive_analysis(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Competitor mention frequency and how well each was handled."""
    tenant_id = str(tenant.id)
    interval = _interval(period)

    query = text(f"""
        SELECT cm->>'name' AS competitor,
               COUNT(*) AS mentions,
               SUM(CASE WHEN (cm->>'handled_well')::bool THEN 1 ELSE 0 END) AS handled_well
        FROM interactions,
             jsonb_array_elements(insights->'competitor_mentions') AS cm
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
          AND insights ? 'competitor_mentions'
          AND jsonb_typeof(insights->'competitor_mentions') = 'array'
          AND cm->>'name' IS NOT NULL
        GROUP BY cm->>'name'
        ORDER BY mentions DESC
        LIMIT 20
    """)
    rows = (await db.execute(query, {"tenant_id": tenant_id})).fetchall()
    return [
        CompetitorRow(
            competitor=row[0],
            mentions=int(row[1]),
            handled_well=int(row[2] or 0),
            handled_well_pct=(
                round((int(row[2] or 0) / int(row[1])) * 100, 1) if row[1] else 0.0
            ),
        )
        for row in rows
    ]


@router.get(
    "/analytics/topics",
    response_model=List[TopicTrend],
    dependencies=[Depends(require_active_subscription)],
)
async def topics_trend(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Topic frequency over a period with pct_change vs. the prior equal window."""
    tenant_id = str(tenant.id)
    interval = _interval(period)

    # Current window
    current_q = text(f"""
        SELECT topic->>'name' AS name,
               SUM(COALESCE((topic->>'mentions')::int, 1)) AS mentions,
               AVG((topic->>'relevance')::float) AS avg_relevance
        FROM interactions,
             jsonb_array_elements(insights->'topics') AS topic
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
          AND insights ? 'topics'
          AND jsonb_typeof(insights->'topics') = 'array'
          AND topic->>'name' IS NOT NULL
        GROUP BY topic->>'name'
        ORDER BY mentions DESC
        LIMIT 20
    """)
    current = {
        row[0]: (int(row[1]), float(row[2]) if row[2] is not None else None)
        for row in (await db.execute(current_q, {"tenant_id": tenant_id})).fetchall()
    }

    # Prior equal-length window
    prior_q = text(f"""
        SELECT topic->>'name' AS name,
               SUM(COALESCE((topic->>'mentions')::int, 1)) AS mentions
        FROM interactions,
             jsonb_array_elements(insights->'topics') AS topic
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}' * 2
          AND created_at <  NOW() - INTERVAL '{interval}'
          AND insights ? 'topics'
          AND jsonb_typeof(insights->'topics') = 'array'
          AND topic->>'name' IS NOT NULL
        GROUP BY topic->>'name'
    """)
    prior = {
        row[0]: int(row[1])
        for row in (await db.execute(prior_q, {"tenant_id": tenant_id})).fetchall()
    }

    out: List[TopicTrend] = []
    for name, (mentions, rel) in current.items():
        prev = prior.get(name, 0)
        if prev > 0:
            pct = round(((mentions - prev) / prev) * 100, 1)
        elif mentions > 0:
            pct = 100.0
        else:
            pct = 0.0
        out.append(TopicTrend(name=name, mentions=mentions, avg_relevance=rel, pct_change=pct))
    return out


@router.get(
    "/analytics/product-feedback",
    response_model=List[ProductFeedbackTheme],
    dependencies=[Depends(require_active_subscription)],
)
async def product_feedback(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Aggregate product_feedback by theme with sentiment counts and a sample quote."""
    tenant_id = str(tenant.id)
    interval = _interval(period)

    query = text(f"""
        SELECT pf->>'theme' AS theme,
               SUM(CASE WHEN pf->>'sentiment' = 'positive' THEN 1 ELSE 0 END) AS pos,
               SUM(CASE WHEN pf->>'sentiment' = 'negative' THEN 1 ELSE 0 END) AS neg,
               SUM(CASE WHEN pf->>'sentiment' = 'neutral'  THEN 1 ELSE 0 END) AS neu,
               MAX(pf->>'quote') AS sample_quote
        FROM interactions,
             jsonb_array_elements(insights->'product_feedback') AS pf
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
          AND insights ? 'product_feedback'
          AND jsonb_typeof(insights->'product_feedback') = 'array'
          AND pf->>'theme' IS NOT NULL
        GROUP BY pf->>'theme'
        ORDER BY (pos + neg + neu) DESC
        LIMIT 20
    """)
    rows = (await db.execute(query, {"tenant_id": tenant_id})).fetchall()
    return [
        ProductFeedbackTheme(
            theme=row[0],
            positive_count=int(row[1] or 0),
            negative_count=int(row[2] or 0),
            neutral_count=int(row[3] or 0),
            sample_quote=row[4],
        )
        for row in rows
    ]


@router.get(
    "/analytics/coaching",
    response_model=CoachingInsights,
    dependencies=[Depends(require_active_subscription)],
)
async def coaching_insights(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Tenant-wide coaching metrics: script adherence, top gaps and improvements."""
    tenant_id = str(tenant.id)
    interval = _interval(period)

    adherence_q = text(f"""
        SELECT AVG((insights->'coaching'->>'script_adherence_score')::float)
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
          AND insights->'coaching' ? 'script_adherence_score'
    """)
    adherence_row = (await db.execute(adherence_q, {"tenant_id": tenant_id})).fetchone()
    avg_adherence = float(adherence_row[0]) if adherence_row and adherence_row[0] is not None else None

    def _list_agg(field: str) -> str:
        return f"""
            SELECT item AS text, COUNT(*) AS cnt
            FROM interactions,
                 jsonb_array_elements_text(insights->'coaching'->'{field}') AS item
            WHERE tenant_id = :tenant_id
              AND created_at >= NOW() - INTERVAL '{interval}'
              AND jsonb_typeof(insights->'coaching'->'{field}') = 'array'
            GROUP BY item
            ORDER BY cnt DESC
            LIMIT 10
        """

    gaps_rows = (await db.execute(text(_list_agg("compliance_gaps")), {"tenant_id": tenant_id})).fetchall()
    improvements_rows = (await db.execute(text(_list_agg("improvements")), {"tenant_id": tenant_id})).fetchall()
    strengths_rows = (await db.execute(text(_list_agg("what_went_well")), {"tenant_id": tenant_id})).fetchall()

    return CoachingInsights(
        avg_script_adherence=avg_adherence,
        top_compliance_gaps=[{"text": r[0], "count": int(r[1])} for r in gaps_rows],
        top_improvements=[{"text": r[0], "count": int(r[1])} for r in improvements_rows],
        top_strengths=[{"text": r[0], "count": int(r[1])} for r in strengths_rows],
    )


@router.get(
    "/analytics/signals",
    response_model=SignalBuckets,
    dependencies=[Depends(require_active_subscription)],
)
async def risk_signals(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Churn and upsell signal distribution over the period."""
    tenant_id = str(tenant.id)
    interval = _interval(period)

    bucket_q = text(f"""
        SELECT
            insights->>'churn_risk_signal' AS churn,
            insights->>'upsell_signal' AS upsell,
            COUNT(*) AS cnt
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
        GROUP BY insights->>'churn_risk_signal', insights->>'upsell_signal'
    """)
    churn_counts: Dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}
    upsell_counts: Dict[str, int] = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for row in (await db.execute(bucket_q, {"tenant_id": tenant_id})).fetchall():
        c, u, cnt = row[0], row[1], int(row[2])
        if c in churn_counts:
            churn_counts[c] += cnt
        if u in upsell_counts:
            upsell_counts[u] += cnt

    avg_q = text(f"""
        SELECT AVG((insights->>'churn_risk')::float),
               AVG((insights->>'upsell_score')::float)
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
    """)
    avg_row = (await db.execute(avg_q, {"tenant_id": tenant_id})).fetchone()
    avg_churn = float(avg_row[0]) if avg_row and avg_row[0] is not None else None
    avg_upsell = float(avg_row[1]) if avg_row and avg_row[1] is not None else None

    channel_q = text(f"""
        SELECT channel,
               SUM(CASE WHEN insights->>'churn_risk_signal' IN ('high','medium') THEN 1 ELSE 0 END) AS churn_flags,
               SUM(CASE WHEN insights->>'upsell_signal'     IN ('high','medium') THEN 1 ELSE 0 END) AS upsell_flags,
               COUNT(*) AS total
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
        GROUP BY channel
        ORDER BY total DESC
    """)
    by_channel = [
        {
            "channel": row[0],
            "churn_flags": int(row[1] or 0),
            "upsell_flags": int(row[2] or 0),
            "total": int(row[3]),
        }
        for row in (await db.execute(channel_q, {"tenant_id": tenant_id})).fetchall()
    ]

    return SignalBuckets(
        churn=churn_counts,
        upsell=upsell_counts,
        avg_churn_risk=avg_churn,
        avg_upsell_score=avg_upsell,
        by_channel=by_channel,
    )


@router.get("/analytics/dashboard", response_model=DashboardSummary)
async def dashboard(
    period: str = Query("30d", pattern="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """One-shot dashboard card data with vs. prior-period deltas."""
    tenant_id = str(tenant.id)
    interval = _interval(period)

    current_q = text(f"""
        SELECT
            COUNT(*) AS total,
            AVG((insights->>'sentiment_score')::float) AS avg_sentiment,
            AVG(s.total_score) AS avg_qa
        FROM interactions i
        LEFT JOIN interaction_scores s ON s.interaction_id = i.id
        WHERE i.tenant_id = :tenant_id
          AND i.created_at >= NOW() - INTERVAL '{interval}'
    """)
    cur = (await db.execute(current_q, {"tenant_id": tenant_id})).fetchone()

    prior_q = text(f"""
        SELECT
            COUNT(*) AS total,
            AVG((insights->>'sentiment_score')::float) AS avg_sentiment,
            AVG(s.total_score) AS avg_qa
        FROM interactions i
        LEFT JOIN interaction_scores s ON s.interaction_id = i.id
        WHERE i.tenant_id = :tenant_id
          AND i.created_at >= NOW() - INTERVAL '{interval}' * 2
          AND i.created_at <  NOW() - INTERVAL '{interval}'
    """)
    prev = (await db.execute(prior_q, {"tenant_id": tenant_id})).fetchone()

    ai_q = text("""
        SELECT COUNT(*) FROM action_items
        WHERE tenant_id = :tenant_id AND status IN ('pending','in_progress')
    """)
    ai_row = (await db.execute(ai_q, {"tenant_id": tenant_id})).fetchone()

    def _delta(a, b):
        if a is None or b is None or b == 0:
            return None
        return round(((a - b) / b) * 100, 1)

    cur_total = int(cur[0] or 0)
    prev_total = int(prev[0] or 0)
    cur_sent = float(cur[1]) if cur and cur[1] is not None else None
    prev_sent = float(prev[1]) if prev and prev[1] is not None else None
    cur_qa = float(cur[2]) if cur and cur[2] is not None else None
    prev_qa = float(prev[2]) if prev and prev[2] is not None else None

    return DashboardSummary(
        total_interactions=cur_total,
        avg_sentiment_score=cur_sent,
        action_items_open=int(ai_row[0] or 0) if ai_row else 0,
        avg_qa_score=cur_qa,
        prev_period_deltas={
            "total_interactions_pct": _delta(cur_total, prev_total),
            "avg_sentiment_pct": _delta(cur_sent, prev_sent),
            "avg_qa_pct": _delta(cur_qa, prev_qa),
        },
    )


@router.get("/analytics/tenant-insights", response_model=List[TenantInsightRow])
async def tenant_insights_list(
    limit: int = Query(12, ge=1, le=52),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Periodic cross-call rollups stored by the weekly aggregation job.

    The tenant_insights table is populated by a Celery beat job; on
    fresh tenants it may be empty (or, in some staging environments,
    not yet migrated) — return [] rather than 500.
    """
    tenant_id = str(tenant.id)
    query = text("""
        SELECT id, period_start, period_end, insights, created_at
        FROM tenant_insights
        WHERE tenant_id = :tenant_id
        ORDER BY period_start DESC NULLS LAST, created_at DESC
        LIMIT :limit
    """)
    try:
        rows = (await db.execute(query, {"tenant_id": tenant_id, "limit": limit})).fetchall()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return []
    return [
        TenantInsightRow(
            id=row[0],
            period_start=str(row[1]) if row[1] else None,
            period_end=str(row[2]) if row[2] else None,
            insights=row[3] or {},
            created_at=str(row[4]),
        )
        for row in rows
    ]


# ── Continuous AI improvement: AI health, vocab pending, reply quality ──


class AiHealth(BaseModel):
    quality_score_avg_7d: Optional[float]
    quality_score_avg_30d: Optional[float]
    feedback_events_7d: int
    asr_wer_7d: Optional[float]
    pending_vocab_candidates: int
    flagged_for_review_count: int


async def _scalar_or_default(db: AsyncSession, sql: str, params: Dict, default):
    """Run ``sql`` returning a single scalar; swallow missing-table /
    empty-row errors and return ``default`` instead.

    Several of the AI-improvement tables (insight_quality_scores,
    wer_metrics, vocabulary_candidates) are populated by background jobs
    that don't run on a fresh sandbox tenant. A missing table or no rows
    must surface as a zero baseline, not a 500.
    """
    try:
        row = (await db.execute(text(sql), params)).fetchone()
    except Exception as exc:  # pragma: no cover — defensive
        import logging as _logging
        _logging.getLogger(__name__).debug("analytics scalar fallback: %s", exc)
        # Roll back so subsequent statements on the same session don't
        # see "current transaction is aborted" after a relation lookup
        # failure.
        try:
            await db.rollback()
        except Exception:
            pass
        return default
    if row is None or row[0] is None:
        return default
    return row[0]


@router.get("/analytics/ai-health", response_model=AiHealth)
async def ai_health(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Per-tenant AI health snapshot — composite quality, WER, feedback velocity.

    Each underlying table is queried independently so a fresh tenant
    (no insight_quality_scores rows, no wer_metrics, ...) returns a
    zero-baseline AiHealth payload instead of a 500.
    """
    tenant_id = str(tenant.id)
    params = {"t": tenant_id}

    q7 = await _scalar_or_default(
        db,
        "SELECT AVG(score) FROM insight_quality_scores "
        "WHERE tenant_id = :t AND created_at >= NOW() - INTERVAL '7 days'",
        params,
        None,
    )
    q30 = await _scalar_or_default(
        db,
        "SELECT AVG(score) FROM insight_quality_scores "
        "WHERE tenant_id = :t AND created_at >= NOW() - INTERVAL '30 days'",
        params,
        None,
    )
    fb = await _scalar_or_default(
        db,
        "SELECT COUNT(*) FROM feedback_events "
        "WHERE tenant_id = :t AND created_at >= NOW() - INTERVAL '7 days'",
        params,
        0,
    )
    wer = await _scalar_or_default(
        db,
        "SELECT AVG(word_error_rate) FROM wer_metrics "
        "WHERE tenant_id = :t AND period_end >= CURRENT_DATE - INTERVAL '14 days'",
        params,
        None,
    )
    vocab = await _scalar_or_default(
        db,
        "SELECT COUNT(*) FROM vocabulary_candidates "
        "WHERE tenant_id = :t AND status = 'pending'",
        params,
        0,
    )
    flagged = await _scalar_or_default(
        db,
        "SELECT COUNT(*) FROM interactions "
        "WHERE tenant_id = :t AND status = 'flagged_for_review'",
        params,
        0,
    )

    return AiHealth(
        quality_score_avg_7d=float(q7) if q7 is not None else None,
        quality_score_avg_30d=float(q30) if q30 is not None else None,
        feedback_events_7d=int(fb or 0),
        asr_wer_7d=float(wer) if wer is not None else None,
        pending_vocab_candidates=int(vocab or 0),
        flagged_for_review_count=int(flagged or 0),
    )


class VocabPendingRow(BaseModel):
    id: uuid.UUID
    term: str
    confidence: str
    source: Optional[str]
    occurrence_count: int


@router.get("/analytics/vocabulary-pending", response_model=List[VocabPendingRow])
async def vocabulary_pending(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    # vocabulary_candidates is populated by an async vocab discovery job
    # — return [] on fresh tenants instead of 500ing the dashboard.
    try:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT id, term, confidence, source, occurrence_count
                    FROM vocabulary_candidates
                    WHERE tenant_id = :t AND status = 'pending'
                    ORDER BY occurrence_count DESC, created_at DESC
                    LIMIT 50
                    """
                ),
                {"t": str(tenant.id)},
            )
        ).fetchall()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return []
    return [
        VocabPendingRow(
            id=row[0],
            term=row[1],
            confidence=row[2],
            source=row[3],
            occurrence_count=int(row[4] or 0),
        )
        for row in rows
    ]


class ReplyQualityRow(BaseModel):
    period: str
    sample_size: int
    avg_similarity: Optional[float]
    pct_sent_unchanged: Optional[float]
    avg_quality_score: Optional[float]


@router.get("/analytics/reply-quality", response_model=List[ReplyQualityRow])
async def reply_quality(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Reply edit-distance + LLM-judge quality, bucketed weekly.

    Empty/missing feedback_events or insight_quality_scores tables on a
    fresh tenant return [] rather than 500.
    """
    try:
        rows = (
            await db.execute(
                text(
                    """
                    WITH events AS (
                        SELECT
                            date_trunc('week', created_at) AS wk,
                            COUNT(*) AS n,
                            AVG(NULLIF((payload ->> 'similarity')::float, NULL)) AS avg_sim,
                            AVG(
                                CASE WHEN event_type = 'reply_sent_unchanged'
                                     THEN 1.0 ELSE 0.0 END
                            ) AS pct_unchanged
                        FROM feedback_events
                        WHERE tenant_id = :t
                          AND event_type IN ('reply_sent_unchanged', 'reply_edited_before_send')
                          AND created_at >= NOW() - INTERVAL '12 weeks'
                        GROUP BY 1
                    ),
                    quality AS (
                        SELECT date_trunc('week', created_at) AS wk,
                               AVG(score) AS qavg
                        FROM insight_quality_scores
                        WHERE tenant_id = :t
                          AND surface = 'email_reply'
                          AND created_at >= NOW() - INTERVAL '12 weeks'
                        GROUP BY 1
                    )
                    SELECT
                        e.wk::date,
                        e.n,
                        e.avg_sim,
                        e.pct_unchanged,
                        q.qavg
                    FROM events e
                    LEFT JOIN quality q USING (wk)
                    ORDER BY e.wk ASC
                    """
                ),
                {"t": str(tenant.id)},
            )
        ).fetchall()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return []
    return [
        ReplyQualityRow(
            period=str(row[0]),
            sample_size=int(row[1] or 0),
            avg_similarity=float(row[2]) if row[2] is not None else None,
            pct_sent_unchanged=float(row[3]) if row[3] is not None else None,
            avg_quality_score=float(row[4]) if row[4] is not None else None,
        )
        for row in rows
    ]
