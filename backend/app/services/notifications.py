"""Per-user notification service.

Drives the in-app notification bell + email digest. Designed to be
called from anywhere that wants to alert a user (action item
assigned, new comment, reject-and-return, scorecard review).

Never raises — notifications must not fail the user-facing operation
they piggyback on.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Notification

logger = logging.getLogger(__name__)


class NotificationKind:
    """String constants matching the Phase 5B-6 migration's CHECK vocabulary."""

    ACTION_ITEM_ASSIGNED = "action_item_assigned"
    ACTION_ITEM_COMMENT = "action_item_comment"
    ACTION_ITEM_RETURNED = "action_item_returned"
    ACTION_ITEM_DUE_SOON = "action_item_due_soon"
    ACTION_ITEM_OVERDUE = "action_item_overdue"
    MANAGER_REVIEW_COMPLETED = "manager_review_completed"
    SCORECARD_REVIEW_ASSIGNED = "scorecard_review_assigned"
    SYSTEM = "system"
    OTHER = "other"


VALID_KINDS = frozenset(
    v for k, v in vars(NotificationKind).items()
    if not k.startswith("_") and isinstance(v, str)
)


async def notify(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str,
    title: str,
    body: Optional[str] = None,
    link_url: Optional[str] = None,
    action_item_id: Optional[uuid.UUID] = None,
    interaction_id: Optional[uuid.UUID] = None,
) -> Optional[Notification]:
    """Insert a notification row. Returns the inserted row, or None on failure.

    Caller controls the surrounding transaction — we don't flush so the
    notification is rolled back together with whatever user-facing
    operation triggered it.
    """
    if kind not in VALID_KINDS:
        logger.warning("Unknown notification kind: %r — using 'other'", kind)
        kind = NotificationKind.OTHER
    try:
        row = Notification(
            tenant_id=tenant_id,
            user_id=user_id,
            kind=kind,
            title=title[:200],
            body=body,
            link_url=link_url[:500] if link_url else None,
            action_item_id=action_item_id,
            interaction_id=interaction_id,
        )
        db.add(row)
        return row
    except Exception:
        logger.exception(
            "Notification insert failed (kind=%s, user_id=%s)", kind, user_id
        )
        return None


async def mark_read(
    db: AsyncSession,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    """Flag one notification as read. Returns True on success."""
    try:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True, read_at=now)
        )
        return result.rowcount > 0
    except Exception:
        logger.exception("Notification mark_read failed")
        return False


async def mark_all_read(
    db: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID
) -> int:
    """Mark every unread notification for the user as read.

    Returns the count of notifications updated.
    """
    try:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.tenant_id == tenant_id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True, read_at=now)
        )
        return result.rowcount or 0
    except Exception:
        logger.exception("Notification mark_all_read failed")
        return 0
