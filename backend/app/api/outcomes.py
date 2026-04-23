"""Proxy-outcome ingestion — production-grade webhook surface.

Hardened contract over the v1 endpoint:

- **``outcome_type`` is a ``Literal[...]``.** Unknown types 400 instead of
  being silently stored and ignored downstream.
- **Idempotency key (``event_id``) is required** when present in the payload.
  Repeated deliveries with the same ``(tenant_id, event_id)`` return 200
  without re-applying the event.  Payloads without an ``event_id`` are
  accepted (for backfill scripts and manual curl) but are rejected on
  retry by a hash fingerprint in the dead-letter log.
- **HMAC verification** — tenants with ``outcomes_hmac_secret`` set must
  include a valid ``X-Linda-Signature: sha256=<hex>`` header.
  Tenants without a secret accept unsigned calls (for gradual rollout).
- **Semantic floors** — ``occurred_at`` cannot be > 1 day in the future;
  if an ``interaction_id`` is referenced it must belong to the caller's
  tenant or the event is dead-lettered.
- **Dead-letter log** — every dropped event is persisted to
  ``dropped_outcome_events`` with the failure reason so integrators can
  debug without losing data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.app.auth import get_current_tenant
from backend.app.db import get_db
from backend.app.models import (
    DroppedOutcomeEvent,
    InteractionFeatures,
    OutcomeEventIngestion,
    Tenant,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Enum of accepted outcome types ───────────────────────────────────────
# Keep this list aligned with ``services/calibration.py`` —
# ``DEFAULT_CALIBRATION_CONFIGS`` consumes every key the calibrator can
# map onto a positive/negative outcome.

OutcomeType = Literal[
    "customer_replied",
    "customer_no_reply_72h",
    "customer_escalated",
    "contact_churned_30d",
    "contact_active_30d",
    "tenant_renewed",
    "tenant_upgraded",
    "tenant_churned",
    "action_item_closed",
    "action_item_closure_rate",
    "deal_won",
    "deal_lost",
]


# ── Request shapes ───────────────────────────────────────────────────────


class OutcomeEvent(BaseModel):
    """One observed downstream event tied to an interaction.

    ``event_id`` is strongly recommended: if present, idempotency is
    enforced per ``(tenant_id, event_id)``.  Without it, the caller is
    responsible for avoiding duplicates.
    """

    interaction_id: uuid.UUID
    outcome_type: OutcomeType
    value: Optional[float] = None
    occurred_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None
    event_id: Optional[str] = Field(default=None, max_length=128)


class OutcomeBatch(BaseModel):
    events: List[OutcomeEvent]


class OutcomeAck(BaseModel):
    accepted: int
    duplicate: int
    dropped: int


# ── HMAC verification ────────────────────────────────────────────────────


def _verify_hmac(secret: Optional[str], body: bytes, signature_header: Optional[str]) -> bool:
    """Return True when the signature is valid, OR when the tenant has no
    secret configured (opt-in rollout).

    Expected header: ``X-Linda-Signature: sha256=<hex>``.
    """
    if not secret:
        return True
    if not signature_header:
        return False
    # Tolerate the optional ``sha256=`` prefix and any surrounding whitespace.
    provided = signature_header.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256=") :]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


# ── Dead-letter helpers ──────────────────────────────────────────────────


async def _deadletter(
    db: AsyncSession,
    tenant_id: Optional[uuid.UUID],
    reason: str,
    payload: Any,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    row = DroppedOutcomeEvent(
        tenant_id=tenant_id,
        reason=reason,
        payload=_to_jsonable(payload),
        headers_snapshot=json.dumps(headers or {}, default=str)[:2000],
    )
    db.add(row)


def _to_jsonable(obj: Any) -> Dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return obj
    return {"value": str(obj)}


# ── Core apply ───────────────────────────────────────────────────────────


async def _apply_events(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    events: List[OutcomeEvent],
    headers_snapshot: Dict[str, str],
) -> OutcomeAck:
    """Apply a batch of events.  Returns counts per outcome.

    For each event: validate semantic floors, check idempotency, update
    ``InteractionFeatures.proxy_outcomes``, record the ingestion row, or
    dead-letter on any failure.  Never raises — callers always get a
    meaningful count back.
    """
    now = datetime.now(timezone.utc)
    future_limit = now + timedelta(days=1)
    accepted = duplicate = dropped = 0

    for event in events:
        # Semantic floor: occurred_at cannot be in the far future.
        if event.occurred_at and event.occurred_at > future_limit:
            dropped += 1
            await _deadletter(db, tenant_id, "future_timestamp", event, headers_snapshot)
            continue

        # Resolve interaction.  interaction_id is required on the schema
        # but we re-check tenant scoping here so cross-tenant writes die
        # on the floor with a clear dead-letter reason.
        stmt = select(InteractionFeatures).where(
            InteractionFeatures.interaction_id == event.interaction_id,
            InteractionFeatures.tenant_id == tenant_id,
        )
        features_row = (await db.execute(stmt)).scalar_one_or_none()
        if features_row is None:
            dropped += 1
            await _deadletter(db, tenant_id, "interaction_not_found", event, headers_snapshot)
            continue

        # Idempotency check.
        if event.event_id:
            existing_stmt = select(OutcomeEventIngestion).where(
                OutcomeEventIngestion.tenant_id == tenant_id,
                OutcomeEventIngestion.event_id == event.event_id,
            )
            existing = (await db.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                duplicate += 1
                continue

        # Apply to features.  Multiple events of the same outcome_type
        # append so late corrections (churned then retained, etc.) stay
        # visible to the calibrator.
        outcomes = dict(features_row.proxy_outcomes or {})
        record = {
            "value": event.value,
            "occurred_at": (event.occurred_at or now).isoformat(),
            "metadata": event.metadata or {},
        }
        existing_value = outcomes.get(event.outcome_type)
        if existing_value is None:
            outcomes[event.outcome_type] = record
        elif isinstance(existing_value, list):
            existing_value.append(record)
            outcomes[event.outcome_type] = existing_value
        else:
            outcomes[event.outcome_type] = [existing_value, record]
        features_row.proxy_outcomes = outcomes
        flag_modified(features_row, "proxy_outcomes")

        # Record the ingestion (enforces idempotency via unique index).
        ingestion = OutcomeEventIngestion(
            tenant_id=tenant_id,
            event_id=event.event_id or _autogen_event_id(event),
            outcome_type=event.outcome_type,
            interaction_id=event.interaction_id,
            payload=event.model_dump(mode="json"),
        )
        db.add(ingestion)
        try:
            await db.flush()
            accepted += 1
        except IntegrityError:
            await db.rollback()
            duplicate += 1

    return OutcomeAck(accepted=accepted, duplicate=duplicate, dropped=dropped)


def _autogen_event_id(event: OutcomeEvent) -> str:
    """Fingerprint an ``event_id``-less payload so retries dedupe.

    Hash of ``(interaction_id, outcome_type, occurred_at)`` — if a caller
    re-sends the same event without an explicit ``event_id`` we still
    catch it.  Collisions across legitimately distinct events with the
    same key tuple are possible but expected to be vanishingly rare.
    """
    key = f"{event.interaction_id}|{event.outcome_type}|{event.occurred_at or ''}"
    return "auto:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/outcomes", response_model=OutcomeAck, status_code=202)
async def ingest_outcome(
    request: Request,
    payload: OutcomeEvent,
    signature: Optional[str] = Header(None, alias="X-Linda-Signature"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    body = await request.body()
    if not _verify_hmac(tenant.outcomes_hmac_secret, body, signature):
        await _deadletter(
            db, tenant.id, "hmac_signature_invalid", payload, dict(request.headers)
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")

    ack = await _apply_events(db, tenant.id, [payload], dict(request.headers))
    await db.commit()
    return ack


@router.post("/outcomes/batch", response_model=OutcomeAck, status_code=202)
async def ingest_outcomes_batch(
    request: Request,
    payload: OutcomeBatch,
    signature: Optional[str] = Header(None, alias="X-Linda-Signature"),
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    body = await request.body()
    if not _verify_hmac(tenant.outcomes_hmac_secret, body, signature):
        await _deadletter(
            db, tenant.id, "hmac_signature_invalid", payload, dict(request.headers)
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")

    ack = await _apply_events(db, tenant.id, payload.events, dict(request.headers))
    await db.commit()
    return ack


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


@router.get("/outcomes/dead-letter/recent")
async def dead_letter_recent(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
) -> List[Dict[str, Any]]:
    """Admin-facing tail of the dead-letter log for this tenant."""
    from sqlalchemy import desc

    stmt = (
        select(DroppedOutcomeEvent)
        .where(DroppedOutcomeEvent.tenant_id == tenant.id)
        .order_by(desc(DroppedOutcomeEvent.received_at))
        .limit(max(1, min(limit, 500)))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "reason": r.reason,
            "payload": r.payload or {},
            "received_at": r.received_at.isoformat(),
        }
        for r in rows
    ]
