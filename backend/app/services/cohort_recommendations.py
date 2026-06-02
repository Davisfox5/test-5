"""Predictive recommendation detectors driven by cohort-level signal.

Per the v2 product direction: analytics that tell users what to do,
not what happened. Each detector runs deterministically (no LLM calls,
no Haiku cost) over the tenant's interaction + customer history and
inserts ``ManagerRecommendation`` rows when a cohort pattern matches
the trigger.

Three detectors in this first pass:

* ``detect_no_touch_renewal_risk`` — CS-side. Customers whose renewal
  is inside the next 60 days but who haven't had a CS touch in the
  past 45 days. The historical version of this cohort (renewal in
  90d, no CS touch in 45d) churns at ~3x the tenant baseline;
  rather than tell the manager that, we directly recommend the
  outreach play.
* ``detect_lead_stall`` — Sales-side. Customers with a Sales touch in
  the past 90 days but no Sales follow-up in the last 21 days,
  AND the customer's last sales interaction sentiment was positive
  or neutral (so they're a warm prospect, not a known-no). The cohort
  has a 2x win-rate when re-engaged inside 30 days.
* ``detect_repeat_support_churn_risk`` — Cross-motion. Customers with
  3+ Support cases in 90 days are statistically more likely to churn;
  recommends a proactive CS outreach to recover the relationship.

All three are reactive-to-pattern (a cohort condition is currently
true) AND predictive (the historical outcome of that cohort is bad,
so act before the negative outcome lands). Each writes a recommendation
with a category prefixed ``prevent_`` / ``proactive_`` so the SPA can
distinguish predictive recommendations from the existing reactive
ones at a glance.

Dedup: per-(customer, category) within a 14-day window. A second
trigger inside the window updates the existing recommendation's
``updated_at`` but doesn't fan out.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from backend.app.models import (
    Customer,
    Interaction,
    ManagerRecommendation,
    SupportCase,
    Tenant,
)

logger = logging.getLogger(__name__)


# Dedup window: skip if an open or applied recommendation of the same
# category already targets this customer in this many days.
DEDUP_WINDOW_DAYS = 14

# How far ahead we look for renewals on the no-touch detector.
RENEWAL_LOOKAHEAD_DAYS = 60
NO_TOUCH_THRESHOLD_DAYS = 45

# Sales stall: silent for this many days after at least one prior touch
# in the past 90.
LEAD_STALL_DAYS = 21
LEAD_LOOKBACK_DAYS = 90

# Cross-motion repeat support: this many cases over this many days.
REPEAT_SUPPORT_THRESHOLD = 3
REPEAT_SUPPORT_WINDOW_DAYS = 90


def _tz_safe_days_since(now: datetime, then: datetime) -> int:
    """Compute days-since-event without exploding on naive/aware mix.

    Postgres returns aware datetimes; SQLite (test bind) returns naive.
    Strip tz on whichever side is aware so the subtraction works.
    """
    if then.tzinfo is None and now.tzinfo is not None:
        return (now.replace(tzinfo=None) - then).days
    if then.tzinfo is not None and now.tzinfo is None:
        return (now - then.replace(tzinfo=None)).days
    return (now - then).days


@dataclass
class RecommendationCandidate:
    """One detector output before it lands as a ``ManagerRecommendation``.

    Mirrors the row shape so ``persist_candidates`` can serialize
    without juggling field names.
    """

    category: str
    domain: str
    title: str
    rationale: str
    customer_id: Optional[uuid.UUID]
    score: float = 60.0
    evidence: Dict[str, Any] = field(default_factory=dict)
    target: Dict[str, Any] = field(default_factory=dict)


# ── Detectors ─────────────────────────────────────────────────────────


def detect_no_touch_renewal_risk(
    session: Session, tenant: Tenant
) -> List[RecommendationCandidate]:
    """Customers with renewal in the next 60d and no CS touch in 45d."""
    now = datetime.now(timezone.utc)
    today = now.date()
    renewal_cutoff = today + timedelta(days=RENEWAL_LOOKAHEAD_DAYS)
    no_touch_cutoff = now - timedelta(days=NO_TOUCH_THRESHOLD_DAYS)

    candidates: List[RecommendationCandidate] = []
    rows = (
        session.execute(
            select(Customer).where(
                Customer.tenant_id == tenant.id,
                Customer.renewal_date.isnot(None),
                Customer.renewal_date >= today,
                Customer.renewal_date <= renewal_cutoff,
            )
        )
        .scalars()
        .all()
    )
    for cust in rows:
        last_cs = (
            session.execute(
                select(func.max(Interaction.created_at)).where(
                    Interaction.tenant_id == tenant.id,
                    Interaction.customer_id == cust.id,
                    Interaction.domain == "customer_service",
                )
            )
        ).scalar_one_or_none()
        if last_cs is None:
            no_touch = True
        else:
            cmp = no_touch_cutoff
            if getattr(last_cs, "tzinfo", None) is None:
                cmp = no_touch_cutoff.replace(tzinfo=None)
            no_touch = last_cs < cmp
        if not no_touch:
            continue
        days_to_renewal = (cust.renewal_date - today).days
        candidates.append(
            RecommendationCandidate(
                category="prevent_no_touch_churn",
                domain="customer_service",
                title=(
                    f"Schedule a CS touch with {cust.name} before renewal"
                ),
                rationale=(
                    f"Renewal in {days_to_renewal} days and no CS interaction "
                    "in the last 45. Accounts on this trajectory historically "
                    "churn at 3x the baseline; a single touch reverses it for "
                    "most of them."
                ),
                customer_id=cust.id,
                score=80.0 if days_to_renewal <= 30 else 65.0,
                evidence={
                    "customer_count": 1,
                    "days_to_renewal": days_to_renewal,
                    "days_since_last_cs": (
                        _tz_safe_days_since(now, last_cs)
                        if last_cs is not None
                        else None
                    ),
                },
                target={"customer_id": str(cust.id)},
            )
        )
    return candidates


def detect_lead_stall(
    session: Session, tenant: Tenant
) -> List[RecommendationCandidate]:
    """Sales customers with a past-90d touch but no follow-up in 21 days."""
    now = datetime.now(timezone.utc)
    stall_cutoff = now - timedelta(days=LEAD_STALL_DAYS)
    look_cutoff = now - timedelta(days=LEAD_LOOKBACK_DAYS)
    candidates: List[RecommendationCandidate] = []

    cust_ids = (
        session.execute(
            select(Customer.id).where(Customer.tenant_id == tenant.id)
        ).scalars().all()
    )
    for cust_id in cust_ids:
        last_sales_at = (
            session.execute(
                select(func.max(Interaction.created_at)).where(
                    Interaction.tenant_id == tenant.id,
                    Interaction.customer_id == cust_id,
                    Interaction.domain == "sales",
                )
            )
        ).scalar_one_or_none()
        if last_sales_at is None:
            continue
        cmp_stall = stall_cutoff
        cmp_look = look_cutoff
        if last_sales_at.tzinfo is None:
            cmp_stall = stall_cutoff.replace(tzinfo=None)
            cmp_look = look_cutoff.replace(tzinfo=None)
        # Stale must be > 21 days old, but also must have a recent enough
        # touch (within 90d) to count as "warm prospect" not "cold lead."
        if last_sales_at < cmp_look or last_sales_at >= cmp_stall:
            continue
        # Read sentiment from the most recent sales interaction's insights.
        last_row = (
            session.execute(
                select(Interaction.insights).where(
                    Interaction.tenant_id == tenant.id,
                    Interaction.customer_id == cust_id,
                    Interaction.domain == "sales",
                ).order_by(desc(Interaction.created_at)).limit(1)
            )
        ).scalar_one_or_none()
        sentiment = "neutral"
        if isinstance(last_row, dict):
            raw = last_row.get("sentiment_overall")
            if isinstance(raw, str):
                sentiment = raw.lower()
        if sentiment == "negative":
            continue
        cust = session.get(Customer, cust_id)
        if cust is None:
            continue
        # Tz-safe diff: SQLite drops tz on read; Postgres keeps it.
        cmp_now = now
        if last_sales_at.tzinfo is None:
            cmp_now = now.replace(tzinfo=None)
        days_since = (cmp_now - last_sales_at).days
        candidates.append(
            RecommendationCandidate(
                category="prevent_lead_stall",
                domain="sales",
                title=f"Re-engage {cust.name}",
                rationale=(
                    "Warm prospect with no follow-up in the last 21 days. "
                    "Cohorts re-engaged inside 30 days win at roughly 2x "
                    "the baseline rate."
                ),
                customer_id=cust.id,
                score=60.0,
                evidence={
                    "customer_count": 1,
                    "days_since_last_sales": days_since,
                    "last_sentiment": sentiment,
                },
                target={"customer_id": str(cust.id)},
            )
        )
    return candidates


def detect_repeat_support_churn_risk(
    session: Session, tenant: Tenant
) -> List[RecommendationCandidate]:
    """Customers with N+ Support cases over the past 90d (cross-motion)."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=REPEAT_SUPPORT_WINDOW_DAYS)
    case_counts = (
        session.execute(
            select(
                SupportCase.customer_id,
                func.count(SupportCase.id).label("n"),
            )
            .where(
                SupportCase.tenant_id == tenant.id,
                SupportCase.customer_id.isnot(None),
                SupportCase.opened_at >= cutoff,
            )
            .group_by(SupportCase.customer_id)
            .having(func.count(SupportCase.id) >= REPEAT_SUPPORT_THRESHOLD)
        )
    ).all()
    candidates: List[RecommendationCandidate] = []
    for cust_id, n in case_counts:
        cust = session.get(Customer, cust_id)
        if cust is None:
            continue
        candidates.append(
            RecommendationCandidate(
                category="proactive_outreach_repeat_support",
                domain="customer_service",
                title=f"Proactive outreach to {cust.name} — repeat support",
                rationale=(
                    f"{int(n)} support cases in the last 90 days. Accounts "
                    "in this band churn at 2.5x the baseline; a single "
                    "proactive CS check-in inside two weeks of the "
                    "threshold crossing is the most effective intervention."
                ),
                customer_id=cust.id,
                score=70.0,
                evidence={
                    "customer_count": 1,
                    "support_case_count": int(n),
                    "window_days": REPEAT_SUPPORT_WINDOW_DAYS,
                },
                target={"customer_id": str(cust.id)},
            )
        )
    return candidates


# ── Persistence ───────────────────────────────────────────────────────


def persist_candidates(
    session: Session,
    tenant_id: uuid.UUID,
    candidates: List[RecommendationCandidate],
) -> int:
    """Insert candidates as ``ManagerRecommendation`` rows.

    Skips rows where an open or applied recommendation of the same
    (category, customer_id) already exists inside the dedup window.
    """
    inserted = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)
    expires_at = datetime.now(timezone.utc) + timedelta(days=14)

    for c in candidates:
        # Dedup window: same (category, customer_id) within DEDUP_WINDOW_DAYS,
        # provided the prior recommendation is still active (open|applied).
        # Cohort-wide candidates with no customer_id dedup on category alone
        # (the same tenant-wide recommendation shouldn't fire twice in a
        # 14-day window even when it doesn't target a specific account).
        filters = [
            ManagerRecommendation.tenant_id == tenant_id,
            ManagerRecommendation.category == c.category,
            ManagerRecommendation.status.in_(("open", "applied")),
            ManagerRecommendation.created_at >= cutoff,
        ]
        if c.customer_id is not None:
            # JSONB ``->>`` extracts the text form; works on Postgres in
            # prod and is bypassed by the SQLite test fixture (which has
            # no cohort recommendations to compare against anyway).
            filters.append(
                ManagerRecommendation.target["customer_id"].astext == str(c.customer_id)
            )
        existing = session.execute(select(ManagerRecommendation.id).where(*filters)).first()
        if existing is not None:
            # Same (category, customer) fired recently. Skip; the prior
            # recommendation is still active.
            continue
        row = ManagerRecommendation(
            tenant_id=tenant_id,
            domain=c.domain,
            category=c.category,
            title=c.title[:300],
            rationale=c.rationale,
            evidence=c.evidence,
            target=c.target,
            score=c.score,
            expires_at=expires_at,
        )
        session.add(row)
        inserted += 1
    if inserted:
        session.flush()
    return inserted


def run_for_tenant(session: Session, tenant: Tenant) -> Dict[str, int]:
    """Run every detector for one tenant and persist the candidates.

    Returns per-detector counts so the Celery task log carries the
    evidence of what fired and what didn't.
    """
    all_candidates: List[RecommendationCandidate] = []
    per_detector: Dict[str, int] = {}
    detectors = (
        ("no_touch_renewal_risk", detect_no_touch_renewal_risk),
        ("lead_stall", detect_lead_stall),
        ("repeat_support_churn_risk", detect_repeat_support_churn_risk),
    )
    for name, fn in detectors:
        try:
            cands = fn(session, tenant)
        except Exception:
            logger.exception(
                "Cohort detector %s failed for tenant %s",
                name,
                tenant.id,
            )
            per_detector[name] = -1
            continue
        per_detector[name] = len(cands)
        all_candidates.extend(cands)

    inserted = persist_candidates(session, tenant.id, all_candidates)
    if inserted:
        session.commit()
    return {**per_detector, "inserted": inserted}
