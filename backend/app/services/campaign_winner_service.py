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

from backend.app.models import Campaign, CampaignEvent, Experiment, Tenant
from backend.app.services.stats import wilson_interval

logger = logging.getLogger(__name__)

POSITIVE_EVENTS = ("reply", "click", "convert")

# A variant needs at least this many sends before it's eligible to win.  A
# 1/1 (100%) variant is noise next to a 990/1000 (99%) variant — ranking on
# raw rate lets tiny samples masquerade as the best performer.  Below this
# floor we skip the whole group and let a later run (with more data)
# re-decide it.
MIN_SENDS_PER_VARIANT = 30


def _engagement_rate(session: Session, campaign: Campaign) -> Tuple[int, int, float]:
    sent = max(int(campaign.sent_count or 0), 0)
    if sent == 0:
        return 0, 0, 0.0
    pos = (
        session.query(func.count(CampaignEvent.id))
        .filter(CampaignEvent.campaign_id == campaign.id)
        .filter(CampaignEvent.event_type.in_(POSITIVE_EVENTS))
        .scalar()
    ) or 0
    return sent, pos, pos / sent


def decide_active_campaigns(session: Session) -> Dict[str, Any]:
    """Group campaigns by ``name`` (within tenant) and pick the variant winner.

    Only acts on groups with ≥ 2 variants AND at least one campaign that's
    ended.  Idempotent — won't write a duplicate Experiment row for a group
    that's already been decided.
    """
    from backend.app.tenant_ctx import tenant_context

    # Campaign/CampaignEvent are tenant-scoped (RLS-protected); read each
    # tenant's candidates under its own bound context, then group.
    groups: Dict[Tuple[Any, str], List[Campaign]] = {}
    tenants = session.query(Tenant).all()
    for tenant in tenants:
        with tenant_context(tenant.id, session):
            candidates = (
                session.query(Campaign)
                .filter(Campaign.tenant_id == tenant.id)
                .filter(Campaign.variant.isnot(None))
                .filter(Campaign.ended_at.isnot(None))
                .all()
            )
            for c in candidates:
                groups.setdefault((c.tenant_id, c.name), []).append(c)

    decided = 0
    skipped = 0

    for (tenant_id, name), siblings in groups.items():
        with tenant_context(tenant_id, session):
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

            per_variant: List[Tuple[str, int, float, float, Campaign]] = []
            for c in siblings:
                sent, pos, rate = _engagement_rate(session, c)
                rate_lower_bound, _upper = wilson_interval(pos, sent)
                per_variant.append((c.variant, sent, rate, rate_lower_bound, c))

            # Only variants with enough sends are eligible to win — otherwise a
            # tiny, lucky sample (e.g. 1/1) could outrank a well-tested one.
            eligible = [r for r in per_variant if r[1] >= MIN_SENDS_PER_VARIANT]
            excluded = len(per_variant) - len(eligible)
            if len(eligible) < 2:
                skipped += 1
                continue

            # Pick the variant with the highest engagement rate we can be
            # confident in (Wilson lower bound), tiebreaker: most-sent.
            eligible.sort(key=lambda r: (r[3], r[1]), reverse=True)
            winner = eligible[0]
            result = {
                "winner_variant": winner[0],
                "winner_sent": winner[1],
                "winner_rate": round(winner[2], 4),
                "winner_rate_lower_bound": round(winner[3], 4),
                "all_variants": [
                    {
                        "variant": v,
                        "sent": s,
                        "rate": round(r, 4),
                        "rate_lower_bound": round(lb, 4),
                    }
                    for v, s, r, lb, _c in per_variant
                ],
            }
            conclusion = (
                f"Variant '{winner[0]}' had the highest engagement rate we can be "
                f"confident in ({winner[3]:.2%} at a 95% confidence level, "
                f"raw rate {winner[2]:.2%}) on a sample of {winner[1]} sends."
            )
            if excluded:
                conclusion += (
                    f" {excluded} variant(s) were excluded for having fewer than "
                    f"{MIN_SENDS_PER_VARIANT} sends — not enough data yet to trust them."
                )
            session.add(
                Experiment(
                    name=f"campaign:{tenant_id}:{name}",
                    type="campaign_variant",
                    status="concluded",
                    hypothesis=f"Find the best-performing variant for campaign '{name}'.",
                    start_date=min(s.started_at for s in siblings if s.started_at) or datetime.utcnow(),
                    end_date=max(s.ended_at for s in siblings if s.ended_at) or datetime.utcnow(),
                    result_summary=result,
                    conclusion=conclusion,
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
