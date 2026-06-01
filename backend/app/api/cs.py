"""Customer Success endpoints.

Renewals strip, account drill-down, and on-demand health recompute.
All routes gate on ``require_domain_agent("customer_service")``;
tenant admins and CS managers pass through.

Health computation is sync-heavy (Python aggregation over the
trailing 90 days of CS interactions); the endpoint runs it on a
per-request sync session adapter rather than blocking on a Celery
job, because the manager portal needs sub-second response and the
data volume per customer is small.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, require_domain_agent
from backend.app.db import get_db
from backend.app.models import Customer

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Pydantic shapes ────────────────────────────────────────────────────


class RenewalRow(BaseModel):
    customer_id: uuid.UUID
    customer_name: str
    renewal_date: date
    health_score: Optional[float]
    onboarding_status: Optional[str]
    renewal_risk_score: float


class HealthBreakdownOut(BaseModel):
    engagement: float
    sentiment: float
    churn_signal: float
    onboarding: float
    renewal_proximity: float
    overall: float
    cs_interaction_count: int
    last_cs_at: Optional[datetime]


class AccountDetailOut(BaseModel):
    customer_id: uuid.UUID
    customer_name: str
    renewal_date: Optional[date]
    onboarding_status: Optional[str]
    health_score: Optional[float]
    health_breakdown: HealthBreakdownOut
    renewal_risk_score: float


class CustomerPatchIn(BaseModel):
    renewal_date: Optional[date] = None
    onboarding_status: Optional[str] = Field(None, max_length=32)


# ── Helpers ────────────────────────────────────────────────────────────


async def _sync_session_from(db: AsyncSession):
    """Return a sync ``Session`` bound to the async session's connection.

    The CS health computation reads through SQLAlchemy ORM cleanly, and
    asyncifying every detector internal would be a refactor for no
    payoff at this volume. Pull the underlying sync session out.
    """
    # AsyncSession exposes the bound sync_session attribute on Postgres
    # async drivers and on the in-memory test bind. ``sync_session`` is
    # the canonical name in SQLAlchemy 2.x.
    sync = getattr(db, "sync_session", None)
    if sync is None:
        raise HTTPException(
            status_code=500,
            detail="CS health: no sync session adapter available.",
        )
    return sync


async def _load_customer(
    db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
) -> Customer:
    c = await db.get(Customer, customer_id)
    if c is None or c.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Customer not found")
    return c


# ── Routes ─────────────────────────────────────────────────────────────


@router.get(
    "/cs/renewals",
    response_model=List[RenewalRow],
)
async def list_renewals(
    days_ahead: int = Query(90, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("customer_service")),
) -> List[RenewalRow]:
    """Customers with renewal dates in the next ``days_ahead`` days,
    sorted soonest-first. Each row carries a renewal-risk composite so
    the CS portal can render a risk badge inline."""
    from backend.app.services.cs_account_health import list_upcoming_renewals

    sync = await _sync_session_from(db)
    rows = list_upcoming_renewals(sync, principal.tenant.id, days_ahead=days_ahead)
    return [
        RenewalRow(
            customer_id=r["customer_id"],  # type: ignore[arg-type]
            customer_name=r["customer_name"],  # type: ignore[arg-type]
            renewal_date=r["renewal_date"],  # type: ignore[arg-type]
            health_score=r["health_score"],  # type: ignore[arg-type]
            onboarding_status=r["onboarding_status"],  # type: ignore[arg-type]
            renewal_risk_score=r["renewal_risk_score"],  # type: ignore[arg-type]
        )
        for r in rows
    ]


@router.get(
    "/cs/accounts/{customer_id}/health",
    response_model=AccountDetailOut,
)
async def get_account_health(
    customer_id: uuid.UUID,
    recompute: bool = Query(
        False,
        description=(
            "Recompute the health score on the fly. False (default) "
            "reads the precomputed ``health_score`` from the row."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("customer_service")),
) -> AccountDetailOut:
    from backend.app.services.cs_account_health import (
        compute_health_score,
        persist_health_score,
        renewal_risk_score,
    )

    c = await _load_customer(db, principal.tenant.id, customer_id)
    sync = await _sync_session_from(db)
    if recompute:
        breakdown = persist_health_score(sync, c)
        # Cross-motion notification: refreshing the score is the
        # natural moment to check whether the account just crossed
        # into the renewal-at-risk band. Best-effort; the helper
        # dedupes against any unread renewal_at_risk for the same
        # account in the last 7 days.
        try:
            from backend.app.services.cs_account_health import (
                should_fire_renewal_at_risk,
            )
            from backend.app.services.notifications import (
                NotificationKind,
                notify,
            )

            if should_fire_renewal_at_risk(sync, c):
                owner_id = c.strongest_connection_user_id
                if owner_id is not None:
                    risk_preview = renewal_risk_score(sync, c)
                    await notify(
                        db,
                        tenant_id=c.tenant_id,
                        user_id=owner_id,
                        kind=NotificationKind.RENEWAL_AT_RISK,
                        title=f"Renewal risk: {c.name}",
                        body=(
                            f"Renewal risk score {risk_preview:.0f}/100"
                            + (
                                f", renews {c.renewal_date.isoformat()}"
                                if c.renewal_date is not None
                                else ""
                            )
                        ),
                        link_url=f"/cs/accounts/{c.id}",
                    )
        except Exception:
            logger.exception(
                "renewal_at_risk notification failed (non-fatal)"
            )
        await db.commit()
    else:
        breakdown = compute_health_score(sync, c)
    risk = renewal_risk_score(sync, c)
    return AccountDetailOut(
        customer_id=c.id,
        customer_name=c.name,
        renewal_date=c.renewal_date,
        onboarding_status=c.onboarding_status,
        health_score=c.health_score,
        health_breakdown=HealthBreakdownOut(
            engagement=breakdown.engagement,
            sentiment=breakdown.sentiment,
            churn_signal=breakdown.churn_signal,
            onboarding=breakdown.onboarding,
            renewal_proximity=breakdown.renewal_proximity,
            overall=breakdown.overall,
            cs_interaction_count=breakdown.cs_interaction_count,
            last_cs_at=breakdown.last_cs_at,
        ),
        renewal_risk_score=risk,
    )


@router.patch(
    "/cs/accounts/{customer_id}",
    response_model=AccountDetailOut,
)
async def patch_customer_cs_fields(
    customer_id: uuid.UUID,
    body: CustomerPatchIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_domain_agent("customer_service")),
) -> AccountDetailOut:
    """Manually set ``renewal_date`` or ``onboarding_status`` from the
    CS portal (e.g. before the CRM sync lands the value, or for tenants
    without an integrated CRM)."""
    c = await _load_customer(db, principal.tenant.id, customer_id)
    valid_onboarding = (
        "not_started",
        "in_progress",
        "stalled",
        "completed",
    )
    if body.onboarding_status is not None:
        if body.onboarding_status not in valid_onboarding:
            raise HTTPException(
                status_code=422,
                detail=f"onboarding_status must be one of {valid_onboarding}",
            )
        c.onboarding_status = body.onboarding_status
    if body.renewal_date is not None:
        c.renewal_date = body.renewal_date
    await db.commit()
    return await get_account_health(
        customer_id=customer_id,
        recompute=False,
        db=db,
        principal=principal,
    )
