"""Cross-customer Customer-Success trend detector.

Same generalization as ``sales_trend_detector.py`` — see that module's
docstring for the full pipeline narrative — applied to the
``customer_service`` domain: several accounts converging on the same
product gap / friction point in a short window is worth a CS manager's
attention before it shows up as churn.

Corpus: ``customer_service``-domain ``Interaction`` rows from the
trailing ``LOOKBACK_DAYS``, folded into one representative string per
interaction via the same
``interaction_trend_corpus.representative_text`` helper the Sales
caller uses (both domains share the same ``insights`` JSONB surface).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Interaction, Tenant
from backend.app.services import trend_engine
from backend.app.services.cohort_recommendations import (
    RecommendationCandidate,
    persist_candidates,
)
from backend.app.services.interaction_trend_corpus import representative_text

logger = logging.getLogger(__name__)

DOMAIN = "customer_service"
ALERT_KIND = "cs_trend_detected"
RECOMMENDATION_CATEGORY = "address_cs_trend"

LOOKBACK_DAYS = trend_engine.GROWTH_WINDOW_DAYS * 2 + 7


def _title(t: trend_engine.EmergingTrend) -> str:
    return (
        f"CS trend: {t.sample_texts[0][:80]}"
        if t.sample_texts
        else "Emerging customer-success trend"
    )


def _body(t: trend_engine.EmergingTrend) -> str:
    return (
        f"{t.recent_count} customer-success conversations in the last two "
        f"weeks raised this, up from {t.prior_count} the two weeks before. "
        f"{t.customer_count} customer"
        f"{'s' if t.customer_count != 1 else ''} affected."
    )


async def run_for_tenant(session: Session, tenant: Tenant) -> Dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    rows = (
        session.execute(
            select(Interaction).where(
                Interaction.tenant_id == tenant.id,
                Interaction.domain == DOMAIN,
                Interaction.created_at >= cutoff,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"clusters": 0, "trends_found": 0, "alerts_inserted": 0, "recs_inserted": 0}

    clusters = await trend_engine.cluster_corpus(
        rows,
        text_fn=lambda r: representative_text(r.insights),
        timestamp_fn=lambda r: r.created_at,
        customer_id_fn=lambda r: r.customer_id,
        source_id_fn=lambda r: r.id,
    )
    trends = trend_engine.find_emerging_trends(clusters)

    alerts_inserted = trend_engine.persist_alerts(
        session,
        tenant.id,
        trends,
        kind=ALERT_KIND,
        domain=DOMAIN,
        title_fn=_title,
        body_fn=_body,
    )
    candidates = [
        RecommendationCandidate(
            category=RECOMMENDATION_CATEGORY,
            domain=DOMAIN,
            title=(
                f"Address emerging customer trend: {t.sample_texts[0][:80]}"
                if t.sample_texts
                else "Address an emerging customer-success trend"
            ),
            rationale=(
                f"{t.recent_count} customer-success conversations in the "
                f"last two weeks from {t.customer_count} customer"
                f"{'s' if t.customer_count != 1 else ''} converged on the "
                "same theme. Worth a proactive note or a product-feedback "
                "loop before it spreads."
            ),
            customer_id=None,
            score=round(t.confidence * 100, 2),
            evidence={
                "recent_count": t.recent_count,
                "prior_count": t.prior_count,
                "growth_ratio": t.growth_ratio,
                "confidence": t.confidence,
                "customer_count": t.customer_count,
                "sample_texts": t.sample_texts[:3],
            },
            target={"cluster_id": t.cluster_id},
        )
        for t in trends
    ]
    inserted_rows = persist_candidates(session, tenant.id, candidates)
    if alerts_inserted or inserted_rows:
        session.commit()
        if inserted_rows:
            from backend.app.services.recommendation_enrichment import (
                queue_enrichment_for,
            )

            queue_enrichment_for(inserted_rows)
    return {
        "clusters": len(clusters),
        "trends_found": len(trends),
        "alerts_inserted": alerts_inserted,
        "recs_inserted": len(inserted_rows),
    }
