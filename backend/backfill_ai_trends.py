#!/usr/bin/env python3
"""Backfill AI analysis trends on existing data.

Three passes, each idempotent:

1. ``--numeric-signals``  Add numeric ``churn_risk`` and ``upsell_score`` to
   any ``interactions.insights`` JSONB that only has the categorical
   ``churn_risk_signal`` / ``upsell_signal``.  Cheap inference by default
   (bucket midpoint); use ``--reanalyze`` to rerun Claude instead.

2. ``--contacts``  Rebuild ``Contact.sentiment_trend``, ``interaction_count``,
   and ``last_seen_at`` from scratch by iterating every contact's
   interactions in chronological order.

3. ``--tenant-insights``  Generate ``TenantInsight`` rows for the last N
   weekly windows (default 12).

With no pass flag, runs all three.  Scope with ``--tenant=<uuid|all>``.

Usage
-----
    python -m backend.backfill_ai_trends --tenant=all
    python -m backend.backfill_ai_trends --tenant=<id> --contacts
    python -m backend.backfill_ai_trends --numeric-signals --reanalyze
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("backfill_ai_trends")


# Inferred numeric values when only the categorical signal is available.
_BUCKET_MIDPOINTS = {
    "high": 0.85,
    "medium": 0.55,
    "low": 0.25,
    "none": 0.05,
}


def _bucket_to_float(signal: Optional[str]) -> Optional[float]:
    if signal is None:
        return None
    return _BUCKET_MIDPOINTS.get(str(signal).lower())


# ── Pass 1: numeric signal backfill ──────────────────────────────────────


def backfill_numeric_signals(
    session: Session,
    tenant_filter: Optional[str],
    reanalyze: bool,
) -> int:
    """Add churn_risk / upsell_score numeric fields to existing insights."""
    from backend.app.models import Interaction

    q = session.query(Interaction)
    if tenant_filter and tenant_filter != "all":
        q = q.filter(Interaction.tenant_id == tenant_filter)

    updated = 0
    for row in q:
        insights = dict(row.insights or {})
        if not insights:
            continue
        changed = False

        if "churn_risk" not in insights or insights.get("churn_risk") is None:
            inferred = _bucket_to_float(insights.get("churn_risk_signal"))
            if inferred is not None:
                insights["churn_risk"] = inferred
                changed = True

        if "upsell_score" not in insights or insights.get("upsell_score") is None:
            inferred = _bucket_to_float(insights.get("upsell_signal"))
            if inferred is not None:
                insights["upsell_score"] = inferred
                changed = True

        if reanalyze:
            logger.warning(
                "--reanalyze not wired yet (would call AIAnalysisService.analyze "
                "on interaction %s); falling back to inference", row.id,
            )

        if changed:
            row.insights = insights
            flag_modified(row, "insights")
            updated += 1

    if updated:
        session.commit()
    logger.info("numeric-signals: updated %d interactions", updated)
    return updated


# ── Pass 2: contact trend backfill ───────────────────────────────────────


def backfill_contact_trends(
    session: Session,
    tenant_filter: Optional[str],
) -> int:
    """Rebuild sentiment_trend / interaction_count / last_seen_at per contact."""
    from backend.app.models import Contact, Interaction

    contact_q = session.query(Contact)
    if tenant_filter and tenant_filter != "all":
        contact_q = contact_q.filter(Contact.tenant_id == tenant_filter)

    processed = 0
    for contact in contact_q:
        interactions = (
            session.query(Interaction)
            .filter(Interaction.contact_id == contact.id)
            .order_by(Interaction.created_at.asc())
            .all()
        )

        # Reset, then replay chronologically using the same helper the live
        # pipeline uses so behavior stays identical.
        from backend.app.tasks import (
            CONTACT_SENTIMENT_TREND_CAP,
            update_contact_rollup,
        )

        contact.sentiment_trend = []
        contact.interaction_count = 0
        contact.last_seen_at = None
        for ix in interactions:
            update_contact_rollup(contact, ix.insights or {}, ix.created_at)
        # update_contact_rollup already enforces the cap; assert for safety.
        assert len(contact.sentiment_trend) <= CONTACT_SENTIMENT_TREND_CAP
        processed += 1

    session.commit()
    logger.info("contacts: rebuilt trend for %d contacts", processed)
    return processed


# ── Pass 3: tenant-level weekly insights backfill ────────────────────────


def backfill_tenant_insights(
    session: Session,
    tenant_filter: Optional[str],
    weeks: int,
) -> int:
    """Populate TenantInsight rows for the last N weekly windows."""
    from backend.app.models import Tenant
    from backend.app.services.tenant_insights_service import rollup_tenant

    if tenant_filter and tenant_filter != "all":
        tenants = session.query(Tenant).filter(Tenant.id == tenant_filter).all()
    else:
        tenants = session.query(Tenant).all()

    total = 0
    today = datetime.utcnow().date()
    for tenant in tenants:
        for w in range(weeks):
            period_end = today - timedelta(days=7 * w)
            period_start = period_end - timedelta(days=7)
            rollup_tenant(session, str(tenant.id), period_start, period_end)
            total += 1
    session.commit()
    logger.info(
        "tenant-insights: wrote %d rollups (%d tenants × %d weeks)",
        total, len(tenants), weeks,
    )
    return total


# ── Entry point ──────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="all", help="tenant UUID or 'all'")
    parser.add_argument("--numeric-signals", action="store_true", help="pass 1 only")
    parser.add_argument("--contacts", action="store_true", help="pass 2 only")
    parser.add_argument("--tenant-insights", action="store_true", help="pass 3 only")
    parser.add_argument("--reanalyze", action="store_true", help="(reserved) rerun Claude")
    parser.add_argument("--weeks", type=int, default=12, help="weeks of tenant-insights")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Lazy import so that --help works without DB settings.
    from backend.app.tasks import _get_sync_session

    run_all = not (args.numeric_signals or args.contacts or args.tenant_insights)
    session = _get_sync_session()
    try:
        summary: Dict[str, Any] = {}
        if run_all or args.numeric_signals:
            summary["numeric_signals_updated"] = backfill_numeric_signals(
                session, args.tenant, args.reanalyze,
            )
        if run_all or args.contacts:
            summary["contacts_rebuilt"] = backfill_contact_trends(
                session, args.tenant,
            )
        if run_all or args.tenant_insights:
            summary["tenant_insights_rollups"] = backfill_tenant_insights(
                session, args.tenant, args.weeks,
            )
        logger.info("Backfill complete: %s", summary)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
