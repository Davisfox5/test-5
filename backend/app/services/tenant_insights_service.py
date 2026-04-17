"""Tenant-level periodic insight rollups.

Aggregates per-interaction ``insights`` JSONB across a period (default last
7 days) into a single ``TenantInsight`` row.  Runs via Celery Beat weekly.
The stored JSONB has five top-level sections: ``sentiment``, ``topics``,
``competitors``, ``product_feedback``, ``coaching``, ``signals``,
``channel_mix`` — matching what the analytics endpoints return on-demand.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _rows_to_dicts(rows, keys: List[str]) -> List[Dict[str, Any]]:
    return [dict(zip(keys, row)) for row in rows]


def aggregate_tenant_period(
    session: Session,
    tenant_id: str,
    period_start: date,
    period_end: date,
) -> Dict[str, Any]:
    """Compute the rolled-up insights dict for one tenant over a date window.

    The window is half-open: ``[period_start, period_end)`` in UTC.  All
    queries scope to ``tenant_id`` and the given window to allow backfill
    over arbitrary historical periods.
    """
    params = {
        "tenant_id": tenant_id,
        "start": period_start,
        "end": period_end,
    }

    # ── Sentiment summary ───────────────────────────────────────────
    sentiment_row = session.execute(
        text("""
            SELECT COUNT(*) AS total,
                   AVG((insights->>'sentiment_score')::float) AS avg_sentiment
            FROM interactions
            WHERE tenant_id = :tenant_id
              AND created_at >= :start
              AND created_at <  :end
        """),
        params,
    ).fetchone()
    total = int(sentiment_row[0] or 0) if sentiment_row else 0
    avg_sentiment = float(sentiment_row[1]) if sentiment_row and sentiment_row[1] is not None else None

    # ── Topics ──────────────────────────────────────────────────────
    topics_rows = session.execute(
        text("""
            SELECT topic->>'name' AS name,
                   SUM(COALESCE((topic->>'mentions')::int, 1)) AS mentions,
                   AVG((topic->>'relevance')::float) AS avg_relevance
            FROM interactions,
                 jsonb_array_elements(insights->'topics') AS topic
            WHERE tenant_id = :tenant_id
              AND created_at >= :start
              AND created_at <  :end
              AND insights ? 'topics'
              AND jsonb_typeof(insights->'topics') = 'array'
              AND topic->>'name' IS NOT NULL
            GROUP BY topic->>'name'
            ORDER BY mentions DESC
            LIMIT 25
        """),
        params,
    ).fetchall()
    topics = [
        {
            "name": r[0],
            "mentions": int(r[1]),
            "avg_relevance": float(r[2]) if r[2] is not None else None,
        }
        for r in topics_rows
    ]

    # ── Competitors ─────────────────────────────────────────────────
    competitor_rows = session.execute(
        text("""
            SELECT cm->>'name' AS competitor,
                   COUNT(*) AS mentions,
                   SUM(CASE WHEN (cm->>'handled_well')::bool THEN 1 ELSE 0 END) AS handled_well
            FROM interactions,
                 jsonb_array_elements(insights->'competitor_mentions') AS cm
            WHERE tenant_id = :tenant_id
              AND created_at >= :start
              AND created_at <  :end
              AND insights ? 'competitor_mentions'
              AND jsonb_typeof(insights->'competitor_mentions') = 'array'
              AND cm->>'name' IS NOT NULL
            GROUP BY cm->>'name'
            ORDER BY mentions DESC
            LIMIT 25
        """),
        params,
    ).fetchall()
    competitors = [
        {
            "competitor": r[0],
            "mentions": int(r[1]),
            "handled_well": int(r[2] or 0),
            "handled_well_pct": round((int(r[2] or 0) / int(r[1])) * 100, 1) if r[1] else 0.0,
        }
        for r in competitor_rows
    ]

    # ── Product feedback ────────────────────────────────────────────
    pf_rows = session.execute(
        text("""
            SELECT pf->>'theme' AS theme,
                   SUM(CASE WHEN pf->>'sentiment' = 'positive' THEN 1 ELSE 0 END) AS pos,
                   SUM(CASE WHEN pf->>'sentiment' = 'negative' THEN 1 ELSE 0 END) AS neg,
                   SUM(CASE WHEN pf->>'sentiment' = 'neutral'  THEN 1 ELSE 0 END) AS neu,
                   MAX(pf->>'quote') AS sample_quote
            FROM interactions,
                 jsonb_array_elements(insights->'product_feedback') AS pf
            WHERE tenant_id = :tenant_id
              AND created_at >= :start
              AND created_at <  :end
              AND insights ? 'product_feedback'
              AND jsonb_typeof(insights->'product_feedback') = 'array'
              AND pf->>'theme' IS NOT NULL
            GROUP BY pf->>'theme'
            ORDER BY (pos + neg + neu) DESC
            LIMIT 25
        """),
        params,
    ).fetchall()
    product_feedback = [
        {
            "theme": r[0],
            "positive_count": int(r[1] or 0),
            "negative_count": int(r[2] or 0),
            "neutral_count": int(r[3] or 0),
            "sample_quote": r[4],
        }
        for r in pf_rows
    ]

    # ── Coaching ────────────────────────────────────────────────────
    adherence_row = session.execute(
        text("""
            SELECT AVG((insights->'coaching'->>'script_adherence_score')::float)
            FROM interactions
            WHERE tenant_id = :tenant_id
              AND created_at >= :start
              AND created_at <  :end
              AND insights->'coaching' ? 'script_adherence_score'
        """),
        params,
    ).fetchone()
    avg_adherence = float(adherence_row[0]) if adherence_row and adherence_row[0] is not None else None

    def _coaching_list(field: str) -> List[Dict[str, Any]]:
        rows = session.execute(
            text(f"""
                SELECT item AS text, COUNT(*) AS cnt
                FROM interactions,
                     jsonb_array_elements_text(insights->'coaching'->'{field}') AS item
                WHERE tenant_id = :tenant_id
                  AND created_at >= :start
                  AND created_at <  :end
                  AND jsonb_typeof(insights->'coaching'->'{field}') = 'array'
                GROUP BY item
                ORDER BY cnt DESC
                LIMIT 10
            """),
            params,
        ).fetchall()
        return [{"text": r[0], "count": int(r[1])} for r in rows]

    coaching = {
        "avg_script_adherence": avg_adherence,
        "top_compliance_gaps": _coaching_list("compliance_gaps"),
        "top_improvements": _coaching_list("improvements"),
        "top_strengths": _coaching_list("what_went_well"),
    }

    # ── Risk signals ────────────────────────────────────────────────
    signals_rows = session.execute(
        text("""
            SELECT insights->>'churn_risk_signal' AS churn,
                   insights->>'upsell_signal' AS upsell,
                   COUNT(*) AS cnt
            FROM interactions
            WHERE tenant_id = :tenant_id
              AND created_at >= :start
              AND created_at <  :end
            GROUP BY insights->>'churn_risk_signal', insights->>'upsell_signal'
        """),
        params,
    ).fetchall()
    churn_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    upsell_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for r in signals_rows:
        if r[0] in churn_counts:
            churn_counts[r[0]] += int(r[2])
        if r[1] in upsell_counts:
            upsell_counts[r[1]] += int(r[2])

    avg_risk_row = session.execute(
        text("""
            SELECT AVG((insights->>'churn_risk')::float),
                   AVG((insights->>'upsell_score')::float)
            FROM interactions
            WHERE tenant_id = :tenant_id
              AND created_at >= :start
              AND created_at <  :end
        """),
        params,
    ).fetchone()
    avg_churn = float(avg_risk_row[0]) if avg_risk_row and avg_risk_row[0] is not None else None
    avg_upsell = float(avg_risk_row[1]) if avg_risk_row and avg_risk_row[1] is not None else None

    signals = {
        "churn": churn_counts,
        "upsell": upsell_counts,
        "avg_churn_risk": avg_churn,
        "avg_upsell_score": avg_upsell,
    }

    # ── Channel mix ─────────────────────────────────────────────────
    channel_rows = session.execute(
        text("""
            SELECT channel,
                   COUNT(*) AS cnt,
                   AVG((insights->>'sentiment_score')::float) AS avg_sentiment
            FROM interactions
            WHERE tenant_id = :tenant_id
              AND created_at >= :start
              AND created_at <  :end
            GROUP BY channel
            ORDER BY cnt DESC
        """),
        params,
    ).fetchall()
    channel_mix = [
        {
            "channel": r[0],
            "count": int(r[1]),
            "avg_sentiment": float(r[2]) if r[2] is not None else None,
        }
        for r in channel_rows
    ]

    return {
        "sentiment": {"total_interactions": total, "avg_sentiment_score": avg_sentiment},
        "topics": topics,
        "competitors": competitors,
        "product_feedback": product_feedback,
        "coaching": coaching,
        "signals": signals,
        "channel_mix": channel_mix,
    }


def rollup_tenant(
    session: Session,
    tenant_id: str,
    period_start: date,
    period_end: date,
) -> Optional[Any]:
    """Compute the rollup and upsert a TenantInsight row.

    If a row already exists for the same ``(tenant_id, period_start,
    period_end)``, it is updated in place; otherwise a new row is inserted.
    Returns the persisted ``TenantInsight`` instance.
    """
    # Imported lazily to avoid circular import at module load.
    from backend.app.models import TenantInsight

    insights_doc = aggregate_tenant_period(session, tenant_id, period_start, period_end)

    existing = (
        session.query(TenantInsight)
        .filter(
            TenantInsight.tenant_id == tenant_id,
            TenantInsight.period_start == period_start,
            TenantInsight.period_end == period_end,
        )
        .first()
    )
    if existing is not None:
        existing.insights = insights_doc
        row = existing
    else:
        row = TenantInsight(
            tenant_id=tenant_id,
            period_start=period_start,
            period_end=period_end,
            insights=insights_doc,
        )
        session.add(row)
    return row


def rollup_all_tenants_weekly(
    session: Session,
    as_of: Optional[datetime] = None,
) -> int:
    """Compute a one-week rollup ending at ``as_of`` for every tenant.

    Defaults to the most recently completed UTC week (Monday–Sunday).
    Returns the number of tenants processed.
    """
    from backend.app.models import Tenant

    if as_of is None:
        as_of = datetime.utcnow()

    period_end = as_of.date()
    period_start = period_end - timedelta(days=7)

    processed = 0
    for tenant in session.query(Tenant).all():
        try:
            rollup_tenant(session, str(tenant.id), period_start, period_end)
            processed += 1
        except Exception:  # noqa: BLE001 — per-tenant isolation
            logger.exception("Failed rollup for tenant %s", tenant.id)
    session.commit()
    logger.info(
        "Weekly tenant rollup complete: %d tenants for %s..%s",
        processed, period_start, period_end,
    )
    return processed
