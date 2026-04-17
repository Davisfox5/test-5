"""Proxy-outcome ingestion — ground truth for every scorer.

Every calibrated scorer in the platform wants observable outcomes to
calibrate against:

- sentiment       → "did the customer reply positively / escalate?"
- churn_risk      → "did the customer cancel within 30/60/90 days?"
- health_score    → "did the tenant renew / upgrade at term?"
- action_items    → "did the action item close in the CRM within 14 days?"

This module provides two write paths:

1. A webhook endpoint (`POST /outcomes`) that external systems (CRM,
   email platform, renewal ops tool) call with an event.  The payload
   is validated and written into
   ``InteractionFeatures.proxy_outcomes`` JSONB.

2. A Celery task (``outcomes_backfill_from_local_data``) that reads
   outcomes we already observe internally — action-item status,
   interaction replies, churn flags — and writes them into the same
   JSONB so calibration works even before a CRM integration lands.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import InteractionFeatures, Tenant

router = APIRouter()


# ── Request shapes ───────────────────────────────────────────────────────


class OutcomeEvent(BaseModel):
    """One observed downstream event tied to an interaction."""

    interaction_id: uuid.UUID
    outcome_type: str = Field(
        ...,
        description=(
            "'customer_replied', 'customer_escalated', 'contact_churned', "
            "'tenant_renewed', 'tenant_upgraded', 'action_item_closed', "
            "'deal_won', 'deal_lost', 'customer_no_reply_72h'."
        ),
    )
    value: Optional[float] = None
    occurred_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None


class OutcomeBatch(BaseModel):
    events: list[OutcomeEvent]


class OutcomeAck(BaseModel):
    accepted: int
    ignored_unknown_interaction: int


# ── Webhook endpoint ─────────────────────────────────────────────────────


@router.post("/outcomes", response_model=OutcomeAck, status_code=202)
async def ingest_outcome(
    payload: OutcomeEvent,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Single-event webhook: CRM / tool posts a single outcome."""
    accepted, ignored = await _apply_events(db, tenant.id, [payload])
    await db.commit()
    return OutcomeAck(accepted=accepted, ignored_unknown_interaction=ignored)


@router.post("/outcomes/batch", response_model=OutcomeAck, status_code=202)
async def ingest_outcomes_batch(
    payload: OutcomeBatch,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Batch webhook — preferred for bulk CRM syncs."""
    accepted, ignored = await _apply_events(db, tenant.id, payload.events)
    await db.commit()
    return OutcomeAck(accepted=accepted, ignored_unknown_interaction=ignored)


async def _apply_events(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    events: list[OutcomeEvent],
) -> tuple[int, int]:
    accepted = 0
    ignored = 0
    for event in events:
        stmt = select(InteractionFeatures).where(
            InteractionFeatures.interaction_id == event.interaction_id,
            InteractionFeatures.tenant_id == tenant_id,
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            ignored += 1
            continue
        # Keyed by outcome_type with a compact event record.  Multiple
        # events of the same type append so late-arriving updates
        # (e.g., churned then retained) stay visible to the calibrator.
        outcomes = dict(row.proxy_outcomes or {})
        record = {
            "value": event.value,
            "occurred_at": (event.occurred_at or datetime.now(timezone.utc)).isoformat(),
            "metadata": event.metadata or {},
        }
        if event.outcome_type in outcomes:
            existing = outcomes[event.outcome_type]
            if isinstance(existing, list):
                existing.append(record)
                outcomes[event.outcome_type] = existing
            else:
                outcomes[event.outcome_type] = [existing, record]
        else:
            outcomes[event.outcome_type] = record
        row.proxy_outcomes = outcomes
        flag_modified(row, "proxy_outcomes")
        accepted += 1
    return accepted, ignored


# ── Retrieval (admin / debugging) ────────────────────────────────────────


@router.get("/outcomes/{interaction_id}")
async def get_outcomes(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> Dict[str, Any]:
    stmt = select(InteractionFeatures).where(
        InteractionFeatures.interaction_id == interaction_id,
        InteractionFeatures.tenant_id == tenant.id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Interaction features not found")
    return row.proxy_outcomes or {}
