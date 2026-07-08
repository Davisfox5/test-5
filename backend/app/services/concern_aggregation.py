"""Cross-customer concern aggregation.

``customer_memory.py`` tracks concerns per customer. This looks ACROSS
customers within a tenant to catch the same underlying worry surfacing
in several accounts around the same time — "several customers just
raised the same concern" — the same growth-detection idea
``support_trend_detector`` applies to Support cases, generalized via
``trend_engine.py`` over concern text instead of case subjects.

Grain: cross-customer, per-tenant. Only ``active`` concerns count as a
"currently live worry" — a concern that's already ``monitoring``,
``resolved``, or ``dormant`` shouldn't fan out a "several customers"
alert for something that's cooling off.

Severity weighting: Workstream A attached a 0-10 ``valence`` reading
(same scale as ``sentiment_score_direct`` — 0 very negative, 10 very
positive) to each concern's evidence entries where one could be
matched. A cluster with the same customer count but a lower average
valence is a sharper signal (customers aren't just mentioning it, they
are upset about it), so ``(10 - valence)`` feeds
``EmergingTrend.total_weight`` and shows up in the alert body as an
average severity reading — never as the raw "valence" term itself (see
``_severity_weight``/the alert body below: plain language only).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import CustomerConcern, Tenant
from backend.app.services import trend_engine

logger = logging.getLogger(__name__)

ALERT_KIND = "customer_concern_trend_detected"

# Only ACTIVE concerns represent a currently-live worry.
_ELIGIBLE_STATUSES = ("active",)

# Neutral default weight when a concern never had a valence reading
# attached (Workstream A's aspect-matching is best-effort) — treated as
# "no signal either way" rather than guessed as severe or mild.
_NEUTRAL_WEIGHT = 5.0


def _latest_valence(concern: CustomerConcern) -> Optional[float]:
    for entry in reversed(concern.evidence or []):
        if isinstance(entry, dict) and isinstance(entry.get("valence"), (int, float)):
            return float(entry["valence"])
    return None


def _severity_weight(concern: CustomerConcern) -> float:
    """``10 - valence``: lower valence (more negative) => higher weight."""
    valence = _latest_valence(concern)
    if valence is None:
        return _NEUTRAL_WEIGHT
    return round(max(0.0, 10.0 - valence), 2)


def _concern_text(concern: CustomerConcern) -> str:
    topic = concern.topic.replace("_", " ")
    if concern.description:
        return f"{topic}: {concern.description}"
    return topic


def _title(t: trend_engine.EmergingTrend) -> str:
    return (
        f"Several customers raised: {t.sample_texts[0][:80]}"
        if t.sample_texts
        else "Several customers raised the same concern"
    )


def _body(t: trend_engine.EmergingTrend) -> str:
    avg_severity = round(t.total_weight / t.recent_count, 1) if t.recent_count else None
    text = (
        f"{t.customer_count} customer"
        f"{'s' if t.customer_count != 1 else ''} have an open concern about "
        "this in the last two weeks."
    )
    if avg_severity is not None:
        text += f" Average severity {avg_severity}/10."
    text += " Worth a proactive note before it spreads."
    return text


async def run_for_tenant(session: Session, tenant: Tenant) -> Dict[str, Any]:
    concerns = (
        session.execute(
            select(CustomerConcern).where(
                CustomerConcern.tenant_id == tenant.id,
                CustomerConcern.status.in_(_ELIGIBLE_STATUSES),
            )
        )
        .scalars()
        .all()
    )
    if not concerns:
        return {"clusters": 0, "trends_found": 0, "alerts_inserted": 0}

    clusters = await trend_engine.cluster_corpus(
        concerns,
        text_fn=_concern_text,
        timestamp_fn=lambda c: c.first_seen_at,
        customer_id_fn=lambda c: c.customer_id,
        source_id_fn=lambda c: c.id,
        weight_fn=_severity_weight,
    )
    trends = trend_engine.find_emerging_trends(clusters)
    # Cross-customer signal: require >= 2 distinct customers, not just
    # a cluster that happens to group several of the same customer's
    # near-duplicate topics together.
    trends = [t for t in trends if t.customer_count >= 2]

    alerts_inserted = trend_engine.persist_alerts(
        session,
        tenant.id,
        trends,
        kind=ALERT_KIND,
        domain="customer_service",
        title_fn=_title,
        body_fn=_body,
    )
    if alerts_inserted:
        session.commit()
    return {
        "clusters": len(clusters),
        "trends_found": len(trends),
        "alerts_inserted": alerts_inserted,
    }
