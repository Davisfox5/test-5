"""Analytics API — aggregated metrics, trends, and team performance."""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import Tenant

router = APIRouter()


# ── Pydantic Schemas ─────────────────────────────────────


class ChannelBreakdown(BaseModel):
    channel: str
    count: int


class BusinessHealth(BaseModel):
    health_score: float
    total_interactions: int
    avg_sentiment: Optional[float]
    channels_breakdown: List[ChannelBreakdown]
    top_topics: List[Dict]


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


# ── Endpoints ────────────────────────────────────────────


@router.get("/analytics/business", response_model=BusinessHealth)
async def business_health(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Business health overview computed from last 30 days of interactions."""
    tenant_id = str(tenant.id)

    # Total interactions & avg sentiment in last 30 days
    summary_query = text("""
        SELECT
            COUNT(*) AS total_interactions,
            AVG((insights->>'overall_sentiment')::float) AS avg_sentiment
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '30 days'
    """)
    summary_result = await db.execute(summary_query, {"tenant_id": tenant_id})
    summary_row = summary_result.fetchone()

    total_interactions = summary_row[0] if summary_row else 0
    avg_sentiment = float(summary_row[1]) if summary_row and summary_row[1] is not None else None

    # Channel breakdown
    channel_query = text("""
        SELECT channel, COUNT(*) AS count
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '30 days'
        GROUP BY channel
        ORDER BY count DESC
    """)
    channel_result = await db.execute(channel_query, {"tenant_id": tenant_id})
    channels_breakdown = [
        ChannelBreakdown(channel=row[0], count=row[1])
        for row in channel_result.fetchall()
    ]

    # Top topics from insights JSON
    topics_query = text("""
        SELECT topic, COUNT(*) AS cnt
        FROM interactions,
             jsonb_array_elements_text(insights->'topics') AS topic
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '30 days'
          AND insights->'topics' IS NOT NULL
        GROUP BY topic
        ORDER BY cnt DESC
        LIMIT 10
    """)
    topics_result = await db.execute(topics_query, {"tenant_id": tenant_id})
    top_topics = [
        {"topic": row[0], "count": row[1]}
        for row in topics_result.fetchall()
    ]

    # Compute health score: simple heuristic based on volume and sentiment
    if avg_sentiment is not None:
        # Normalize sentiment from [-1,1] to [0,100] and weight with volume
        health_score = round(min(100.0, max(0.0, (avg_sentiment + 1) * 50)), 1)
    else:
        health_score = 50.0  # neutral when no data

    return BusinessHealth(
        health_score=health_score,
        total_interactions=total_interactions,
        avg_sentiment=avg_sentiment,
        channels_breakdown=channels_breakdown,
        top_topics=top_topics,
    )


@router.get("/analytics/trends", response_model=List[TrendPoint])
async def trends(
    period: str = Query("30d", pattern="^(7d|30d|90d)$", description="Time period: 7d, 30d, or 90d"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Time-series interaction trends grouped by date and channel."""
    tenant_id = str(tenant.id)
    interval_map = {"7d": "7 days", "30d": "30 days", "90d": "90 days"}
    interval = interval_map[period]

    query = text("""
        SELECT
            DATE(created_at) AS date,
            channel,
            COUNT(*) AS interaction_count,
            AVG((insights->>'overall_sentiment')::float) AS avg_sentiment
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL :interval
        GROUP BY DATE(created_at), channel
        ORDER BY date ASC, channel
    """)
    # SQLAlchemy text() doesn't support INTERVAL with bind params directly,
    # so we use string formatting for the interval (safe — validated by regex above).
    query = text(f"""
        SELECT
            DATE(created_at) AS date,
            channel,
            COUNT(*) AS interaction_count,
            AVG((insights->>'overall_sentiment')::float) AS avg_sentiment
        FROM interactions
        WHERE tenant_id = :tenant_id
          AND created_at >= NOW() - INTERVAL '{interval}'
        GROUP BY DATE(created_at), channel
        ORDER BY date ASC, channel
    """)
    result = await db.execute(query, {"tenant_id": tenant_id})

    return [
        TrendPoint(
            date=str(row[0]),
            channel=row[1],
            interaction_count=row[2],
            avg_sentiment=float(row[3]) if row[3] is not None else None,
        )
        for row in result.fetchall()
    ]


@router.get("/analytics/team", response_model=List[AgentStats])
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
            AVG((i.insights->>'overall_sentiment')::float) AS avg_sentiment,
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
    result = await db.execute(query, {"tenant_id": tenant_id})

    return [
        AgentStats(
            agent_id=row[0],
            name=row[1],
            interaction_count=row[2],
            avg_sentiment=float(row[3]) if row[3] is not None else None,
            avg_scorecard_score=float(row[4]) if row[4] is not None else None,
            churn_flags=row[5],
        )
        for row in result.fetchall()
    ]


@router.get("/analytics/agents/{agent_id}", response_model=AgentStats)
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
            AVG((i.insights->>'overall_sentiment')::float) AS avg_sentiment,
            AVG(s.total_score) AS avg_scorecard_score,
            COUNT(CASE WHEN (i.insights->>'churn_risk')::float > 0.7 THEN 1 END) AS churn_flags
        FROM users u
        LEFT JOIN interactions i ON i.agent_id = u.id AND i.tenant_id = :tenant_id
        LEFT JOIN interaction_scores s ON s.interaction_id = i.id
        WHERE u.id = :agent_id
          AND u.tenant_id = :tenant_id
        GROUP BY u.id, u.name
    """)
    result = await db.execute(query, {"tenant_id": tenant_id, "agent_id": str(agent_id)})
    row = result.fetchone()
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


@router.get("/analytics/clients/{contact_id}", response_model=ClientTrends)
async def client_trends(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Per-client sentiment trends, interaction history, and churn risk."""
    tenant_id = str(tenant.id)

    # Verify contact belongs to tenant
    verify_query = text("""
        SELECT id FROM contacts
        WHERE id = :contact_id AND tenant_id = :tenant_id
    """)
    verify_result = await db.execute(verify_query, {"contact_id": str(contact_id), "tenant_id": tenant_id})
    if not verify_result.fetchone():
        raise HTTPException(status_code=404, detail="Contact not found")

    # Sentiment over time
    sentiment_query = text("""
        SELECT
            DATE(created_at) AS date,
            AVG((insights->>'overall_sentiment')::float) AS avg_sentiment
        FROM interactions
        WHERE contact_id = :contact_id
          AND tenant_id = :tenant_id
        GROUP BY DATE(created_at)
        ORDER BY date ASC
    """)
    sentiment_result = await db.execute(sentiment_query, {"contact_id": str(contact_id), "tenant_id": tenant_id})
    sentiment_over_time = [
        {"date": str(row[0]), "avg_sentiment": float(row[1]) if row[1] is not None else None}
        for row in sentiment_result.fetchall()
    ]

    # Interaction history summary
    history_query = text("""
        SELECT id, channel, title, status, created_at
        FROM interactions
        WHERE contact_id = :contact_id
          AND tenant_id = :tenant_id
        ORDER BY created_at DESC
        LIMIT 50
    """)
    history_result = await db.execute(history_query, {"contact_id": str(contact_id), "tenant_id": tenant_id})
    interaction_history = [
        {
            "id": str(row[0]),
            "channel": row[1],
            "title": row[2],
            "status": row[3],
            "created_at": str(row[4]),
        }
        for row in history_result.fetchall()
    ]

    # Compute churn risk from latest interaction's insights
    churn_query = text("""
        SELECT (insights->>'churn_risk')::float
        FROM interactions
        WHERE contact_id = :contact_id
          AND tenant_id = :tenant_id
          AND insights->>'churn_risk' IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
    """)
    churn_result = await db.execute(churn_query, {"contact_id": str(contact_id), "tenant_id": tenant_id})
    churn_row = churn_result.fetchone()
    churn_risk = float(churn_row[0]) if churn_row and churn_row[0] is not None else None

    return ClientTrends(
        contact_id=contact_id,
        sentiment_over_time=sentiment_over_time,
        interaction_history=interaction_history,
        churn_risk=churn_risk,
    )


@router.get("/analytics/competitive", response_model=List[Dict])
async def competitive_analysis(
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Competitive intelligence — placeholder until Claude analysis includes competitor mentions."""
    # TODO: Populate when Claude analysis pipeline extracts competitor mentions
    return []
