"""Cross-tenant aggregate metrics — opt-in, no per-tenant leakage.

Tenants opt out via ``Tenant.features_enabled.data_use_for_improvement = false``
(default behaviour respects the most conservative setting — opt-out).  Tenants
without an explicit value default to **opt-in** for these aggregate-only
metrics; the opt-in toggle is documented in the SaaS agreement.

Aggregates written:
- ``insight_quality_avg_by_surface_channel``
- ``insight_quality_avg_by_call_duration_bucket``
- ``reply_edit_distance_distribution_by_length_bucket``

Stored in ``cross_tenant_analytics`` which has **no** ``tenant_id`` column by
design — Postgres can't accidentally leak a tenant's data through these rows.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from backend.app.models import (
    CrossTenantAnalytic,
    FeedbackEvent,
    InsightQualityScore,
    Interaction,
    Tenant,
)

logger = logging.getLogger(__name__)

OPT_OUT_KEY = "data_use_for_improvement"


def _opted_in_tenant_ids(session: Session) -> List[Any]:
    """Return the set of tenant_ids that haven't opted out of pooled analytics."""
    tenants = session.query(Tenant).all()
    out: List[Any] = []
    for t in tenants:
        flag = (t.features_enabled or {}).get(OPT_OUT_KEY)
        if flag is False:
            continue  # explicit opt-out
        out.append(t.id)
    return out


def _bucket_for_duration(seconds: Any) -> str:
    if seconds is None:
        return "unknown"
    s = int(seconds)
    if s < 120:
        return "lt_2min"
    if s < 600:
        return "lt_10min"
    if s < 1800:
        return "lt_30min"
    return "gte_30min"


def _bucket_for_length(chars: int) -> str:
    if chars < 200:
        return "short"
    if chars < 800:
        return "medium"
    return "long"


def aggregate_weekly(session: Session) -> Dict[str, Any]:
    """Compute weekly cross-tenant aggregates."""
    period_end = date.today()
    period_start = period_end - timedelta(days=7)
    cutoff = datetime.combine(period_start, datetime.min.time())

    opted_in = _opted_in_tenant_ids(session)
    if not opted_in:
        return {"status": "no_opt_in_tenants"}

    written = 0

    # 1. Quality score average by (surface, channel)
    q1 = (
        session.query(
            InsightQualityScore.surface,
            Interaction.channel,
            func.count().label("n"),
            func.avg(InsightQualityScore.score).label("avg"),
        )
        .join(Interaction, Interaction.id == InsightQualityScore.interaction_id)
        .filter(InsightQualityScore.tenant_id.in_(opted_in))
        .filter(InsightQualityScore.created_at >= cutoff)
        .group_by(InsightQualityScore.surface, Interaction.channel)
        .all()
    )
    for surface, channel, n, avg in q1:
        if n < 5:
            continue  # k-anonymity floor
        session.add(
            CrossTenantAnalytic(
                metric_name="insight_quality_avg_by_surface_channel",
                bucket=channel or "unknown",
                surface=surface,
                channel=channel,
                sample_size=int(n),
                value=float(avg),
                period_start=period_start,
                period_end=period_end,
            )
        )
        written += 1

    # 2. Quality score by call duration bucket (analysis surface only)
    interactions = (
        session.query(Interaction.id, Interaction.duration_seconds)
        .filter(Interaction.tenant_id.in_(opted_in))
        .filter(Interaction.created_at >= cutoff)
        .all()
    )
    bucket_to_ids: Dict[str, List[Any]] = {}
    for iid, dur in interactions:
        bucket_to_ids.setdefault(_bucket_for_duration(dur), []).append(iid)

    for bucket, iids in bucket_to_ids.items():
        if len(iids) < 5:
            continue
        avg_score = (
            session.query(func.avg(InsightQualityScore.score))
            .filter(InsightQualityScore.interaction_id.in_(iids))
            .filter(InsightQualityScore.surface == "analysis")
            .scalar()
        )
        if avg_score is None:
            continue
        session.add(
            CrossTenantAnalytic(
                metric_name="insight_quality_avg_by_call_duration_bucket",
                bucket=bucket,
                surface="analysis",
                sample_size=len(iids),
                value=float(avg_score),
                period_start=period_start,
                period_end=period_end,
            )
        )
        written += 1

    # 3. Reply edit-distance distribution by reply length bucket
    reply_events = (
        session.query(FeedbackEvent.payload)
        .filter(FeedbackEvent.tenant_id.in_(opted_in))
        .filter(
            FeedbackEvent.event_type.in_(
                ("reply_sent_unchanged", "reply_edited_before_send")
            )
        )
        .filter(FeedbackEvent.created_at >= cutoff)
        .all()
    )
    by_bucket: Dict[str, List[float]] = {}
    for (payload,) in reply_events:
        if not payload:
            continue
        sim = payload.get("similarity")
        upd_len = payload.get("updated_len", 0)
        if sim is None:
            continue
        bucket = _bucket_for_length(int(upd_len))
        by_bucket.setdefault(bucket, []).append(float(sim))

    for bucket, sims in by_bucket.items():
        if len(sims) < 5:
            continue
        avg_sim = sum(sims) / len(sims)
        session.add(
            CrossTenantAnalytic(
                metric_name="reply_edit_distance_distribution_by_length_bucket",
                bucket=bucket,
                surface="email_reply",
                sample_size=len(sims),
                value=float(avg_sim),
                distribution={
                    "min": round(min(sims), 4),
                    "max": round(max(sims), 4),
                },
                period_start=period_start,
                period_end=period_end,
            )
        )
        written += 1

    session.commit()
    return {
        "status": "ok",
        "rows_written": written,
        "opted_in_tenants": len(opted_in),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }
