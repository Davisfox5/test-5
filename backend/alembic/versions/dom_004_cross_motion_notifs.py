"""Cross-motion notification kinds.

Extends ``notifications.kind`` CHECK with four new values driven by
PR cross-motion-notifications: ``case_assigned`` /
``case_escalated`` / ``renewal_at_risk`` / ``qbr_overdue``. Each is
inserted by a small trigger in the originating service (SupportCase
assign + status change, CS account-health persist) and rendered in
the in-app notification tray.

Revision ID: dom_004_cross_motion_notifs
Revises: dom_003_cs_kb_polish
Create Date: 2026-06-01

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "dom_004_cross_motion_notifs"
down_revision: Union[str, None] = "dom_003_cs_kb_polish"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_NOTIFICATION_KINDS = (
    # ── existing vocabulary preserved ───────────────────────────────
    "action_item_assigned",
    "action_item_comment",
    "action_item_returned",
    "action_item_due_soon",
    "action_item_overdue",
    "manager_review_completed",
    "scorecard_review_assigned",
    "manager_alert",
    "system",
    "other",
    # ── added in this migration ─────────────────────────────────────
    "case_assigned",
    "case_escalated",
    "renewal_at_risk",
    "qbr_overdue",
)


_OLD_NOTIFICATION_KINDS = (
    "action_item_assigned",
    "action_item_comment",
    "action_item_returned",
    "action_item_due_soon",
    "action_item_overdue",
    "manager_review_completed",
    "scorecard_review_assigned",
    "manager_alert",
    "system",
    "other",
)


def upgrade() -> None:
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        "kind IN (" + ", ".join(f"'{k}'" for k in _NEW_NOTIFICATION_KINDS) + ")",
    )


def downgrade() -> None:
    # Forward-only on the data side: a row written with one of the new
    # kinds will fail the restored CHECK. Sweep them to ``other`` so the
    # downgrade doesn't strand orphan rows.
    op.execute(
        "UPDATE notifications "
        "SET kind = 'other' "
        "WHERE kind IN ('case_assigned', 'case_escalated', "
        "'renewal_at_risk', 'qbr_overdue')"
    )
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        "kind IN (" + ", ".join(f"'{k}'" for k in _OLD_NOTIFICATION_KINDS) + ")",
    )
