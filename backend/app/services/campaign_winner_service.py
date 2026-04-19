"""Campaign variant winner-selection.

Reuses the same A/B engine as the prompt variants — campaigns are an outbound
A/B test where the metric is engagement (reply rate / conversion) instead of
quality score.  For each ``Campaign.name`` group, looks at all sibling
campaigns with the same ``name`` but different ``variant`` labels, computes
each variant's engagement rate from ``campaign_events``, and writes an
``Experiment`` row recording the winner.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.models import Campaign, CampaignEvent, Experiment

logger = logging.getLogger(__name__)

POSITIVE_EVENTS = ("reply", "click", "convert")


def _engagement_rate(session: Session, campaign: Campaign) -> Tuple[int, float]:
    sent = max(int(campaign.sent_count or 0), 0)
    if sent == 0:
        return 0, 0.0
    pos = (
        session.query(func.count(CampaignEvent.id))
        .filter(CampaignEvent.campaign_id == campaign.id)
        .filter(CampaignEvent.event_type.in_(POSITIVE_EVENTS))
        .scalar()
    ) or 0
    return sent, pos / sent


def decide_active_campaigns(session: Session) -> Dict[str, Any]:
    """Group campaigns by ``name`` (within tenant) and pick the variant winner.

    Only acts on groups with ≥ 2 variants AND at least one campaign that's
    ended.  Idempotent — won't write a duplicate Experiment row for a group
    that's already been decided.
    """
    candidates = (
        session.query(Campaign)
        .filter(Campaign.variant.isnot(None))
        .filter(Campaign.ended_at.isnot(None))
        .all()
    )

    # Group by (tenant_id, name).
    groups: Dict[Tuple[Any, str], List[Campaign]] = {}
    for c in candidates:
        groups.setdefault((c.tenant_id, c.name), []).append(c)

    decided = 0
    skipped = 0

    for (tenant_id, name), siblings in groups.items():
        if len({s.variant for s in siblings}) < 2:
            skipped += 1
            continue
        # Skip if we already have an Experiment row for this group.
        existing = (
            session.query(Experiment)
            .filter(Experiment.type == "campaign_variant")
            .filter(Experiment.name == f"campaign:{tenant_id}:{name}")
            .first()
        )
        if existing is not None:
            skipped += 1
            continue

        per_variant: List[Tuple[str, int, float, Campaign]] = []
        for c in siblings:
            sent, rate = _engagement_rate(session, c)
            per_variant.append((c.variant, sent, rate, c))

        # Pick the highest-rate variant (tiebreaker: most-sent).
        per_variant.sort(key=lambda r: (r[2], r[1]), reverse=True)
        winner = per_variant[0]
        result = {
            "winner_variant": winner[0],
            "winner_sent": winner[1],
            "winner_rate": round(winner[2], 4),
            "all_variants": [
                {"variant": v, "sent": s, "rate": round(r, 4)}
                for v, s, r, _c in per_variant
            ],
        }
        session.add(
            Experiment(
                name=f"campaign:{tenant_id}:{name}",
                type="campaign_variant",
                status="concluded",
                hypothesis=f"Find the best-performing variant for campaign '{name}'.",
                start_date=min(s.started_at for s in siblings if s.started_at) or datetime.utcnow(),
                end_date=max(s.ended_at for s in siblings if s.ended_at) or datetime.utcnow(),
                result_summary=result,
                conclusion=(
                    f"Variant '{winner[0]}' had the highest engagement rate "
                    f"({winner[2]:.2%}) on a sample of {winner[1]} sends."
                ),
            )
        )
        decided += 1

    if decided:
        session.commit()
    return {
        "groups_inspected": len(groups),
        "decided": decided,
        "skipped": skipped,
    }
