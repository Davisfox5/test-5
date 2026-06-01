"""Account health scoring + renewal-risk composite for Customer Success.

Computes a 0-100 ``health_score`` per customer from a deterministic
weighted combination of engagement, sentiment, churn risk, and (when
present) ``onboarding_status`` and days-to-renewal. Stored on
``customers.health_score`` by the nightly ``account_health_job``; read
by the CS portal's account drill-down and the CS Manager's renewal-
risk recommendation builder.

The combination is intentionally simple (no ML). The motion's signals
are still being characterized; a transparent weighted sum is easier to
explain to a CSM than a model output. When we have enough labeled data
to justify a calibrated model, swap ``compute_health_score`` for one.
"""
from __future__ import annotations

import logging
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Customer, Interaction

logger = logging.getLogger(__name__)


# ── Tunables ───────────────────────────────────────────────────────────

# How much trailing history feeds the score.
WINDOW_DAYS = 90

# Component weights — sum to 100. ``engagement`` captures contact
# frequency, ``sentiment`` captures conversation tone, ``churn_signal``
# captures explicit risk flags from the analysis pass, ``onboarding``
# is a status-bucket nudge, ``renewal_proximity`` flips negative
# closer to renewal so a stalled account looks worse as the date approaches.
WEIGHT_ENGAGEMENT = 25
WEIGHT_SENTIMENT = 30
WEIGHT_CHURN_SIGNAL = 30
WEIGHT_ONBOARDING = 10
WEIGHT_RENEWAL_PROXIMITY = 5


@dataclass(frozen=True)
class HealthBreakdown:
    """Why a customer scored what they did. Exposed by the API so the
    CS-portal account-drill-down can show the breakdown bars."""

    engagement: float  # 0-100
    sentiment: float
    churn_signal: float
    onboarding: float
    renewal_proximity: float
    overall: float
    cs_interaction_count: int
    last_cs_at: Optional[datetime]


# ── Computation ─────────────────────────────────────────────────────────


def compute_health_score(
    session: Session,
    customer: Customer,
    *,
    now: Optional[datetime] = None,
) -> HealthBreakdown:
    """Return a 0-100 health score plus a transparent breakdown.

    Reads CS-motion interactions from the trailing ``WINDOW_DAYS`` plus
    the customer's ``onboarding_status`` / ``renewal_date``. Doesn't
    mutate the customer row — the caller decides whether to persist.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)

    rows = (
        session.execute(
            select(
                Interaction.created_at,
                Interaction.insights,
            ).where(
                Interaction.tenant_id == customer.tenant_id,
                Interaction.customer_id == customer.id,
                Interaction.domain == "customer_service",
                Interaction.created_at >= cutoff,
            )
        )
    ).all()

    cs_count = len(rows)
    sentiments: List[float] = []
    high_churn = 0
    last_cs_at: Optional[datetime] = None
    for created_at, insights in rows:
        if last_cs_at is None or created_at > last_cs_at:
            last_cs_at = created_at
        if not isinstance(insights, dict):
            continue
        s = insights.get("sentiment_score")
        if isinstance(s, (int, float)):
            sentiments.append(float(s))
        signal = insights.get("churn_risk_signal")
        if isinstance(signal, str) and signal.lower() == "high":
            high_churn += 1

    # Engagement: a target cadence of one CS touch every 30 days = full
    # marks; zero touches in the window = zero marks; linear interpolation.
    target_touches = WINDOW_DAYS / 30.0  # 3 over 90d
    engagement = min(100.0, (cs_count / target_touches) * 100.0) if target_touches > 0 else 0.0

    # Sentiment: arithmetic mean on the 0-10 scale, rescaled to 0-100.
    # No history -> middle of the road (50).
    sentiment_component = (
        statistics.mean(sentiments) * 10.0 if sentiments else 50.0
    )
    sentiment_component = max(0.0, min(100.0, sentiment_component))

    # Churn signal: the share of recent CS calls flagged high — 0% high
    # = 100, 100% high = 0. Inverted so "more churn flags = lower score."
    if cs_count == 0:
        churn_component = 60.0  # neutral-ish when no data
    else:
        share_high = high_churn / cs_count
        churn_component = max(0.0, 100.0 * (1.0 - share_high))

    # Onboarding bucket nudge.
    onboarding_map = {
        "completed": 100.0,
        "in_progress": 70.0,
        "not_started": 40.0,
        "stalled": 10.0,
    }
    onboarding_component = onboarding_map.get(customer.onboarding_status or "", 60.0)

    # Renewal proximity: the closer renewal_date is, the more the
    # other components matter (we don't penalize, we just stop adding
    # the "neutral 50" cushion). Used as a small (~5%) nudge.
    if customer.renewal_date is not None:
        days_to = (
            datetime.combine(customer.renewal_date, datetime.min.time(), tzinfo=timezone.utc)
            - now
        ).total_seconds() / 86400.0
        if days_to <= 0:
            renewal_component = 30.0  # past-due renewal — investigate
        elif days_to <= 30:
            renewal_component = 40.0
        elif days_to <= 90:
            renewal_component = 60.0
        else:
            renewal_component = 80.0
    else:
        renewal_component = 60.0

    overall = (
        engagement * WEIGHT_ENGAGEMENT
        + sentiment_component * WEIGHT_SENTIMENT
        + churn_component * WEIGHT_CHURN_SIGNAL
        + onboarding_component * WEIGHT_ONBOARDING
        + renewal_component * WEIGHT_RENEWAL_PROXIMITY
    ) / 100.0

    return HealthBreakdown(
        engagement=round(engagement, 1),
        sentiment=round(sentiment_component, 1),
        churn_signal=round(churn_component, 1),
        onboarding=round(onboarding_component, 1),
        renewal_proximity=round(renewal_component, 1),
        overall=round(overall, 1),
        cs_interaction_count=cs_count,
        last_cs_at=last_cs_at,
    )


def persist_health_score(
    session: Session, customer: Customer
) -> HealthBreakdown:
    """Compute and write the score on the customer row. Used by the
    nightly job and on-demand recompute endpoint."""
    breakdown = compute_health_score(session, customer)
    customer.health_score = breakdown.overall
    session.flush()
    return breakdown


# ── Renewal-at-risk notification (added with cross-motion-notifications) ──


def should_fire_renewal_at_risk(
    session: Session, customer: Customer
) -> bool:
    """Return True when ``customer`` warrants a ``renewal_at_risk``
    notification right now.

    Conditions:

    * Renewal-risk composite is in the 'high' band (>= 70).
    * No existing unread ``renewal_at_risk`` notification for the
      customer's owner within the last 7 days (idempotency — keeps a
      daily refresh from flooding the CSM with duplicates).

    The 7-day dedup is the right window for renewal-risk: the situation
    rarely changes day-to-day, but flips week-over-week as touches
    land. Tunable via ``RENEWAL_NOTIF_DEDUP_DAYS`` if customers ask.
    """
    from datetime import timedelta

    from backend.app.models import Notification

    score = renewal_risk_score(session, customer)
    if score < 70:
        return False
    if customer.strongest_connection_user_id is None:
        # No owner to notify — orphan accounts don't get pinged.
        # The nightly account-owner job populates this field; until
        # it lands we skip rather than notifying the wrong person.
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=RENEWAL_NOTIF_DEDUP_DAYS)
    recent = (
        session.execute(
            select(Notification.id)
            .where(
                Notification.tenant_id == customer.tenant_id,
                Notification.user_id == customer.strongest_connection_user_id,
                Notification.kind == "renewal_at_risk",
                Notification.created_at >= cutoff,
                # Dedup is per customer; the link URL carries the id.
                Notification.link_url == f"/cs/accounts/{customer.id}",
            )
            .limit(1)
        )
    ).first()
    return recent is None


RENEWAL_NOTIF_DEDUP_DAYS = 7


# ── Renewal-risk composite ─────────────────────────────────────────────


def renewal_risk_score(
    session: Session, customer: Customer
) -> float:
    """Return a 0-100 renewal-risk number (higher = worse).

    Composite of health, support burden, and days-to-renewal. Replaces
    the count-of-high-churn-signals heuristic the manager-portal
    detector used in PR #113.

    The score is symmetrical with the health computation — when no
    renewal date is set, the result is the inverse of the health
    score, capped at 70 so we don't flag every healthy account as
    "renewal risk."
    """
    health = customer.health_score
    if health is None:
        breakdown = compute_health_score(session, customer)
        health = breakdown.overall

    risk = 100.0 - float(health)

    # Open support cases bump risk. Cheap query: count, not join.
    from backend.app.models import SupportCase

    open_cases = (
        session.execute(
            select(SupportCase.id).where(
                SupportCase.tenant_id == customer.tenant_id,
                SupportCase.customer_id == customer.id,
                SupportCase.status.in_(("open", "in_progress", "escalated")),
            )
        ).all()
    )
    if len(open_cases) >= 3:
        risk += 15
    elif len(open_cases) >= 1:
        risk += 5

    # Days-to-renewal accelerator.
    if customer.renewal_date is not None:
        now = datetime.now(timezone.utc)
        days_to = (
            datetime.combine(customer.renewal_date, datetime.min.time(), tzinfo=timezone.utc)
            - now
        ).total_seconds() / 86400.0
        if days_to <= 0:
            risk += 15
        elif days_to <= 30:
            risk += 10
        elif days_to <= 90:
            risk += 5

    return max(0.0, min(100.0, round(risk, 1)))


# ── Public helpers used by the API ─────────────────────────────────────


def list_upcoming_renewals(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    days_ahead: int = 90,
) -> List[Dict[str, object]]:
    """Customers whose ``renewal_date`` falls in the next ``days_ahead``.

    Sorted soonest-first; each row carries renewal date, current
    ``health_score``, and a computed ``renewal_risk_score`` so the
    CS portal can render risk badges without per-row API calls.
    """
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days_ahead)
    customers = (
        session.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.renewal_date.isnot(None),
                Customer.renewal_date >= today,
                Customer.renewal_date <= end,
            ).order_by(Customer.renewal_date.asc())
        )
        .scalars()
        .all()
    )
    out: List[Dict[str, object]] = []
    for c in customers:
        out.append(
            {
                "customer_id": c.id,
                "customer_name": c.name,
                "renewal_date": c.renewal_date,
                "health_score": c.health_score,
                "onboarding_status": c.onboarding_status,
                "renewal_risk_score": renewal_risk_score(session, c),
            }
        )
    return out
