"""Phase 0 — intervention event recorder.

Append-only log of rep / manager / system actions that affect a customer's
outcome trajectory. Joined against ``customer_outcome_events`` and
``interaction_features`` at training time to construct unbiased training
tuples — a customer who churns after we flagged them and intervened is
a different signal from one who churns after we flagged them and did
nothing.

The schema has a CHECK constraint on ``kind``; pass values from
``InterventionKind`` to avoid silent constraint failures.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import InterventionEvent

logger = logging.getLogger(__name__)


class InterventionKind:
    """String constants matching the Phase 0 migration's CHECK vocabulary."""

    FOLLOW_UP_SENT = "follow_up_sent"
    MANAGER_REVIEW = "manager_review"
    ESCALATION = "escalation"
    REP_CALLBACK = "rep_callback"
    DISCOUNT_OFFERED = "discount_offered"
    ACTION_ITEM_COMPLETED = "action_item_completed"
    ACTION_ITEM_DISMISSED = "action_item_dismissed"
    ACTION_ITEM_SNOOZED = "action_item_snoozed"
    ACTION_ITEM_REOPENED = "action_item_reopened"
    SCORECARD_REVIEW_COMPLETED = "scorecard_review_completed"
    OTHER = "other"


VALID_KINDS = frozenset(
    v for k, v in vars(InterventionKind).items()
    if not k.startswith("_") and isinstance(v, str)
)


# ── Status → lifecycle kind mapping for action items ─────────────────────


_ACTION_ITEM_STATUS_TO_KIND: Dict[str, str] = {
    "completed": InterventionKind.ACTION_ITEM_COMPLETED,
    "done": InterventionKind.ACTION_ITEM_COMPLETED,
    "dismissed": InterventionKind.ACTION_ITEM_DISMISSED,
    "rejected": InterventionKind.ACTION_ITEM_DISMISSED,
    "snoozed": InterventionKind.ACTION_ITEM_SNOOZED,
    "pending": InterventionKind.ACTION_ITEM_REOPENED,
    "in_progress": InterventionKind.ACTION_ITEM_REOPENED,
    "open": InterventionKind.ACTION_ITEM_REOPENED,
}


def action_item_kind_for_transition(
    old_status: Optional[str], new_status: Optional[str]
) -> Optional[str]:
    """Return the intervention kind for a status transition, or None.

    None when the new status is unknown, when nothing changed, or when
    transitioning into a non-terminal initial state from itself.
    Reopening (back to pending/open from a terminal state) DOES fire as
    ``action_item_reopened`` because that's a meaningful intervention.
    """
    if not new_status:
        return None
    if old_status and old_status.lower() == new_status.lower():
        return None
    return _ACTION_ITEM_STATUS_TO_KIND.get(new_status.lower())


# ── Recording ────────────────────────────────────────────────────────────


async def record_intervention(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str,
    interaction_id: Optional[uuid.UUID] = None,
    customer_id: Optional[uuid.UUID] = None,
    actor_user_id: Optional[uuid.UUID] = None,
    meta: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
) -> Optional[InterventionEvent]:
    """Insert an intervention event. Never raises — bias-correction
    telemetry must never fail a user-facing request.

    Returns the inserted row on success, ``None`` on any failure.
    """
    if kind not in VALID_KINDS:
        logger.warning("Unknown intervention kind: %r — using 'other'", kind)
        meta = {**(meta or {}), "raw_kind": kind}
        kind = InterventionKind.OTHER

    try:
        row = InterventionEvent(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            customer_id=customer_id,
            actor_user_id=actor_user_id,
            kind=kind,
            meta=meta or {},
        )
        if occurred_at is not None:
            row.occurred_at = occurred_at
        db.add(row)
        # Caller is responsible for the commit. We deliberately don't
        # flush here so the row participates in the surrounding
        # transaction — if the user's edit rolls back, the intervention
        # event rolls back with it.
        return row
    except Exception:
        logger.exception(
            "intervention event insert failed (kind=%s, tenant_id=%s)",
            kind, tenant_id,
        )
        return None


async def record_action_item_lifecycle(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    interaction_id: Optional[uuid.UUID],
    action_item_id: uuid.UUID,
    old_status: Optional[str],
    new_status: Optional[str],
    actor_user_id: Optional[uuid.UUID] = None,
    dismiss_reason: Optional[str] = None,
) -> Optional[InterventionEvent]:
    """Convenience wrapper that turns an action item status transition
    into the right ``InterventionKind`` and records it.

    Returns ``None`` (without recording) when the transition is a no-op
    or maps to an unknown status.
    """
    kind = action_item_kind_for_transition(old_status, new_status)
    if kind is None:
        return None

    meta: Dict[str, Any] = {
        "action_item_id": str(action_item_id),
        "old_status": old_status,
        "new_status": new_status,
    }
    if dismiss_reason and kind == InterventionKind.ACTION_ITEM_DISMISSED:
        meta["dismiss_reason"] = dismiss_reason

    return await record_intervention(
        db,
        tenant_id=tenant_id,
        kind=kind,
        interaction_id=interaction_id,
        actor_user_id=actor_user_id,
        meta=meta,
    )
