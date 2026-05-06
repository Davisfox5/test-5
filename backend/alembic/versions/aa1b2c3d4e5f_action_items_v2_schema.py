"""Phase 5B-1 — action items v2 schema.

Pre-launch redesign of the action items surface. Adds the columns needed
for advanced next-step inference (next-step type, recommended channel,
participants, prep artifacts, dependency chains, implicit-signal flags,
manual creation, useful-feedback scoring), simplifies the status enum to
``open | done | dismissed`` (snooze becomes orthogonal via
``snoozed_until``), and creates a ``category_taxonomy`` table that lets
the LLM emit free-form categories now and evolve toward a canonical set
via occurrence-count promotion.

Status normalization (no data migration needed for snoozed_until — that
column is independent and stays as-is):

    pending, in_progress, open       → open
    done,    completed                → done
    dismissed, rejected               → dismissed
    snoozed  → open  (snooze is orthogonal via ``snoozed_until``)

Revision ID: aa1b2c3d4e5f
Revises: z3b4c5d6e7f8
Create Date: 2026-05-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "aa1b2c3d4e5f"
down_revision: Union[str, None] = "z3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEXT_STEP_TYPES = (
    "meeting",
    "phone_call",
    "email",
    "document_send",
    "crm_update",
    "internal_loop_in",
    "other",
)

_RECOMMENDED_CHANNELS = (
    "email",
    "phone_call",
    "meeting",
    "document_send",
)

_STATUS_VALUES = ("open", "done", "dismissed")


def upgrade() -> None:
    # ── New columns on action_items ──────────────────────────────────
    op.add_column(
        "action_items",
        sa.Column("next_step_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("recommended_channel", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("channel_reasoning", sa.Text(), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("participants", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "action_items",
        sa.Column("prep_artifacts", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        "action_items",
        sa.Column(
            "parent_action_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("action_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "action_items",
        sa.Column("implicit_signal", sa.Text(), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("manually_created", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "action_items",
        sa.Column("feedback_score", sa.Integer(), nullable=False, server_default="0"),
    )

    # ── Normalize legacy status values ────────────────────────────────
    op.execute(
        """
        UPDATE action_items
        SET status = CASE
            WHEN status IN ('pending', 'in_progress', 'open', 'snoozed') THEN 'open'
            WHEN status IN ('done', 'completed') THEN 'done'
            WHEN status IN ('dismissed', 'rejected') THEN 'dismissed'
            ELSE 'open'
        END
        """
    )
    # New default is 'open'. server_default change requires alter_column.
    op.alter_column(
        "action_items",
        "status",
        server_default=sa.text("'open'"),
    )

    op.create_index(
        "ix_action_items_status_open",
        "action_items",
        ["tenant_id", "status"],
        postgresql_where=sa.text("status = 'open'"),
    )

    # ── category_taxonomy table ──────────────────────────────────────
    # Per-tenant canonical category set. Tenant-scoped because different
    # verticals (sales vs CS) develop different category vocabularies.
    # Global rows (tenant_id IS NULL) act as defaults for new tenants.
    op.create_table(
        "category_taxonomy",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("canonical_name", sa.String(length=64), nullable=False),
        sa.Column("aliases", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_canonical", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "canonical_name", name="uq_category_taxonomy_tenant_canonical"),
    )
    op.create_index(
        "ix_category_taxonomy_tenant_canonical",
        "category_taxonomy",
        ["tenant_id", "is_canonical"],
    )

    # Seed initial canonical categories. tenant_id NULL = global default.
    op.execute(
        """
        INSERT INTO category_taxonomy
            (tenant_id, canonical_name, aliases, description, is_canonical, promoted_at)
        VALUES
            (NULL, 'follow_up', '["followup", "follow up"]'::jsonb,
             'Generic follow-up — combination of email draft and call script depending on recommended_channel.',
             true, now()),
            (NULL, 'commitment_made', '["promise", "rep_promise"]'::jsonb,
             'Rep promised something specific on the call (deliverable, send-by date, action).',
             true, now()),
            (NULL, 'commitment_owed_by_customer', '["customer_promise"]'::jsonb,
             'Customer promised something (review the proposal, get internal sign-off, share data).',
             true, now()),
            (NULL, 'compliance_remediation', '["compliance", "regulatory"]'::jsonb,
             'A compliance / disclosure gap surfaced on the call that needs follow-up action.',
             true, now()),
            (NULL, 'deal_advance', '["next_step", "advance"]'::jsonb,
             'Move the deal stage forward — schedule next meeting, loop in stakeholders, send proposal.',
             true, now()),
            (NULL, 'escalation', '["escalate", "manager_review"]'::jsonb,
             'Situation requires manager or specialist involvement (SE, legal, customer success).',
             true, now()),
            (NULL, 'discovery_followup', '["discovery_gap"]'::jsonb,
             'A question or topic the rep deferred or never asked, worth revisiting.',
             true, now())
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_category_taxonomy_tenant_canonical",
        table_name="category_taxonomy",
    )
    op.drop_table("category_taxonomy")
    op.drop_index("ix_action_items_status_open", table_name="action_items")
    op.alter_column(
        "action_items",
        "status",
        server_default=sa.text("'pending'"),
    )
    op.drop_column("action_items", "feedback_score")
    op.drop_column("action_items", "manually_created")
    op.drop_column("action_items", "implicit_signal")
    op.drop_column("action_items", "parent_action_item_id")
    op.drop_column("action_items", "prep_artifacts")
    op.drop_column("action_items", "participants")
    op.drop_column("action_items", "channel_reasoning")
    op.drop_column("action_items", "recommended_channel")
    op.drop_column("action_items", "next_step_type")
