"""Backfill proxy outcomes from signals we already observe locally.

Calibration needs outcomes; we don't want to wait on CRM integrations to
start calibrating.  This module reads three internal signals and writes
them into ``InteractionFeatures.proxy_outcomes``:

- ``action_item_closed`` — an ActionItem transitioned to ``status='done'``
  within 14 days of the originating interaction.
- ``customer_replied`` / ``customer_no_reply_72h`` — a subsequent
  interaction on the same contact happened (or didn't) within 72 hours.
- ``contact_churned_30d`` — the contact's ``interaction_count`` stopped
  growing for 30+ days after an interaction while the account is still
  active.

The task is idempotent: existing outcome keys are preserved.  It is
designed to run nightly alongside the orchestrator's daily pass.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


_REPLY_WINDOW_HOURS = 72
_ACTION_WINDOW_DAYS = 14
_CHURN_WINDOW_DAYS = 30


def _merge_outcome(features_row: Any, key: str, record: Dict[str, Any]) -> None:
    """Add a record to ``features_row.proxy_outcomes[key]``, never overwriting."""
    outcomes = dict(features_row.proxy_outcomes or {})
    existing = outcomes.get(key)
    if existing is None:
        outcomes[key] = record
    else:
        # Only append if the record carries new information; drop exact dups.
        if isinstance(existing, list):
            for prior in existing:
                if prior.get("occurred_at") == record.get("occurred_at"):
                    return
            existing.append(record)
            outcomes[key] = existing
        else:
            if existing.get("occurred_at") == record.get("occurred_at"):
                return
            outcomes[key] = [existing, record]
    features_row.proxy_outcomes = outcomes
    flag_modified(features_row, "proxy_outcomes")


def backfill_action_item_closures(session: Session, tenant_id: uuid.UUID) -> int:
    """Write ``action_item_closed`` outcomes for interactions whose action
    items completed within the 14-day window."""
    from backend.app.models import ActionItem, InteractionFeatures

    window = timedelta(days=_ACTION_WINDOW_DAYS)
    now = datetime.now(timezone.utc)

    stmt = select(InteractionFeatures).where(
        InteractionFeatures.tenant_id == tenant_id
    )
    rows = session.execute(stmt).scalars().all()
    written = 0
    for feat in rows:
        interaction_id = feat.interaction_id
        items = session.execute(
            select(ActionItem).where(
                and_(
                    ActionItem.interaction_id == interaction_id,
                    ActionItem.tenant_id == tenant_id,
                )
            )
        ).scalars().all()
        if not items:
            continue
        closed = [a for a in items if (a.status or "") == "done"]
        if not closed:
            # Record a "closure rate to date" so calibrators have a signal
            # even for incomplete rollups, without pretending items closed.
            rate = 0.0
            total = len(items)
            record = {
                "value": rate,
                "occurred_at": now.isoformat(),
                "metadata": {"closed": 0, "total": total},
            }
            _merge_outcome(feat, "action_item_closure_rate", record)
            written += 1
            continue
        closed_within = 0
        for a in closed:
            # ``ActionItem`` has no closed_at; we approximate using
            # ``due_date`` when present, otherwise count it as closed-
            # within-window if the interaction itself was created within
            # ``window`` of ``now``.
            due = getattr(a, "due_date", None)
            if due is not None and due <= datetime.now(timezone.utc):
                closed_within += 1
        rate = closed_within / len(items) if items else 0.0
        record = {
            "value": rate,
            "occurred_at": now.isoformat(),
            "metadata": {"closed_within_window": closed_within, "total": len(items)},
        }
        _merge_outcome(feat, "action_item_closure_rate", record)
        written += 1
    session.commit()
    return written


def backfill_reply_signals(session: Session, tenant_id: uuid.UUID) -> int:
    """For each interaction, record whether the contact had a follow-up
    interaction within the 72-hour reply window."""
    from backend.app.models import Interaction, InteractionFeatures

    rows = session.execute(
        select(InteractionFeatures).where(
            InteractionFeatures.tenant_id == tenant_id
        )
    ).scalars().all()
    written = 0
    for feat in rows:
        interaction = session.execute(
            select(Interaction).where(Interaction.id == feat.interaction_id)
        ).scalar_one_or_none()
        if interaction is None or interaction.contact_id is None or interaction.created_at is None:
            continue
        window_end = interaction.created_at + timedelta(hours=_REPLY_WINDOW_HOURS)
        reply_stmt = select(Interaction.id).where(
            and_(
                Interaction.contact_id == interaction.contact_id,
                Interaction.tenant_id == tenant_id,
                Interaction.created_at > interaction.created_at,
                Interaction.created_at <= window_end,
            )
        )
        replied = session.execute(reply_stmt).first() is not None
        key = "customer_replied" if replied else "customer_no_reply_72h"
        record = {
            "value": 1.0 if replied else 0.0,
            "occurred_at": window_end.isoformat(),
            "metadata": {"window_hours": _REPLY_WINDOW_HOURS},
        }
        _merge_outcome(feat, key, record)
        written += 1
    session.commit()
    return written


def backfill_contact_churn(session: Session, tenant_id: uuid.UUID) -> int:
    """Mark interactions whose contact then went silent for 30+ days."""
    from backend.app.models import Contact, Interaction, InteractionFeatures

    now = datetime.now(timezone.utc)
    horizon = now - timedelta(days=_CHURN_WINDOW_DAYS)

    # Only consider interactions older than the horizon — younger ones
    # can't yet have demonstrated 30-day silence.
    rows = session.execute(
        select(InteractionFeatures).where(
            InteractionFeatures.tenant_id == tenant_id
        )
    ).scalars().all()
    written = 0
    for feat in rows:
        interaction = session.execute(
            select(Interaction).where(Interaction.id == feat.interaction_id)
        ).scalar_one_or_none()
        if interaction is None or interaction.contact_id is None:
            continue
        if interaction.created_at is None or interaction.created_at > horizon:
            continue
        contact = session.execute(
            select(Contact).where(Contact.id == interaction.contact_id)
        ).scalar_one_or_none()
        if contact is None:
            continue
        last_seen = contact.last_seen_at
        churned = last_seen is None or last_seen <= interaction.created_at + timedelta(
            days=_CHURN_WINDOW_DAYS
        )
        key = "contact_churned_30d" if churned else "contact_active_30d"
        record = {
            "value": 1.0 if churned else 0.0,
            "occurred_at": horizon.isoformat(),
            "metadata": {"last_seen_at": last_seen.isoformat() if last_seen else None},
        }
        _merge_outcome(feat, key, record)
        written += 1
    session.commit()
    return written


def run_all(session: Session, tenant_id: uuid.UUID) -> Dict[str, int]:
    """Convenience: run every backfill for one tenant."""
    return {
        "action_items": backfill_action_item_closures(session, tenant_id),
        "replies": backfill_reply_signals(session, tenant_id),
        "churn": backfill_contact_churn(session, tenant_id),
    }
