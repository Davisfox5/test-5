"""Phase 5B-6 — action items mega-backend schema changes.

One migration covering:

1. ``interaction_comments`` gains ``action_item_id`` (nullable FK) and
   ``interaction_id`` becomes nullable. A comment must have at least
   one of the two — pinned with a CHECK. This lets us reuse the
   existing comment table for action item dialogue without a separate
   table; if it doesn't carry the load post-launch we'll migrate to a
   dedicated ``action_item_comments`` table (the path is reversible).

2. ``kb_documents`` gains ``owner_user_id`` (nullable FK to users).
   When populated, the document is the agent's personal KB and is
   visible only to that agent + their managers/admins. NULL keeps the
   document tenant-wide as before. No data migration needed —
   existing rows stay tenant-wide by virtue of NULL.

3. New ``notifications`` table — per-user delivery surface for events
   like 'action item assigned to you', 'comment posted on your action
   item', 'manager review completed', 'reject-and-return'.

4. ``action_items`` gains ``suggested_attachments`` (JSONB list — LLM
   pre-suggested KB docs, rep reviews before send) and
   ``attachments_sent`` (JSONB — what actually went out).

5. ``email_sends`` gains ``attachments`` (JSONB — recorded for audit
   and dedupe; supports both KB-doc references and inline blobs).

Revision ID: ab2c3d4e5f6a
Revises: aa1b2c3d4e5f
Create Date: 2026-05-06
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "ab2c3d4e5f6a"
down_revision: Union[str, None] = "aa1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NOTIFICATION_KINDS = (
    "action_item_assigned",
    "action_item_comment",
    "action_item_returned",  # reject-and-return back to original assigner
    "action_item_due_soon",
    "action_item_overdue",
    "manager_review_completed",
    "scorecard_review_assigned",
    "system",
    "other",
)


def upgrade() -> None:
    # ── 1. interaction_comments: action_item_id + nullable interaction_id ──
    op.add_column(
        "interaction_comments",
        sa.Column(
            "action_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("action_items.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.alter_column(
        "interaction_comments",
        "interaction_id",
        nullable=True,
    )
    op.create_check_constraint(
        "ck_interaction_comments_target",
        "interaction_comments",
        "interaction_id IS NOT NULL OR action_item_id IS NOT NULL",
    )
    op.create_index(
        "ix_interaction_comments_action_item",
        "interaction_comments",
        ["action_item_id"],
    )

    # ── 2. kb_documents: owner_user_id for per-agent KB ─────────────────
    op.add_column(
        "kb_documents",
        sa.Column(
            "owner_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_kb_documents_owner_user",
        "kb_documents",
        ["tenant_id", "owner_user_id"],
    )

    # ── 3. notifications table ──────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("link_url", sa.String(length=500), nullable=True),
        sa.Column(
            "action_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("action_items.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "interaction_id",
            UUID(as_uuid=True),
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "kind IN ("
            + ", ".join(f"'{k}'" for k in _NOTIFICATION_KINDS)
            + ")",
            name="ck_notifications_kind",
        ),
    )
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id", "is_read", "created_at"],
        postgresql_where=sa.text("is_read = false"),
    )
    op.create_index(
        "ix_notifications_tenant",
        "notifications",
        ["tenant_id", "created_at"],
    )

    # ── 4. action_items: suggested_attachments + attachments_sent ──────
    op.add_column(
        "action_items",
        sa.Column(
            "suggested_attachments",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "action_items",
        sa.Column(
            "attachments_sent",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # ── 5. email_sends: attachments ────────────────────────────────────
    op.add_column(
        "email_sends",
        sa.Column(
            "attachments",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("email_sends", "attachments")
    op.drop_column("action_items", "attachments_sent")
    op.drop_column("action_items", "suggested_attachments")
    op.drop_index("ix_notifications_tenant", table_name="notifications")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_kb_documents_owner_user", table_name="kb_documents")
    op.drop_column("kb_documents", "owner_user_id")
    op.drop_index(
        "ix_interaction_comments_action_item",
        table_name="interaction_comments",
    )
    op.drop_constraint(
        "ck_interaction_comments_target",
        "interaction_comments",
        type_="check",
    )
    op.alter_column(
        "interaction_comments",
        "interaction_id",
        nullable=False,
    )
    op.drop_column("interaction_comments", "action_item_id")
