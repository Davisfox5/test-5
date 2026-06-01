"""Customer relationship memory: concerns + their-side commitments.

Adds the data substrate for the living per-customer profile the user
direction described. LINDA writes to ``customer_concerns`` at analysis
time so every motion (Sales, CS, Support) sees the same evolving
picture of what's worrying the customer, what's calmed down, and
what's dormant. ``customer_commitments`` mirrors the existing
``action_items`` table but tracks promises the CUSTOMER made to us
(``"we'll get you the contract by Friday"``), which we currently
don't track anywhere.

Stakeholders aren't a new table here — ``Contact`` already covers the
named-person side; we'll add light annotations on it
(``is_champion`` / ``is_detractor`` flags) in PR 5 alongside the KB
tagging work, where they belong with the customer-scoped retrieval.

Revision ID: dom_006_customer_memory
Revises: dom_005_sso_scim
Create Date: 2026-06-01

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "dom_006_customer_memory"
down_revision: Union[str, None] = "dom_005_sso_scim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    jsonb = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()
    uuid_t = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)

    # ── customer_concerns ──────────────────────────────────────────────
    op.create_table(
        "customer_concerns",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            uuid_t,
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Short canonical topic — ``pricing`` / ``data_security`` /
        # ``integration_xyz``. Free-form so a new topic doesn't need a
        # schema change; the extractor normalizes through a small
        # taxonomy and falls back to the model's freeform string.
        sa.Column("topic", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # active: in play right now. monitoring: surfaced, currently
        # quiet but worth watching. resolved: explicitly settled by a
        # later interaction. dormant: not mentioned in N days — the
        # nightly job auto-transitions active to dormant.
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "severity",
            sa.String(length=16),
            nullable=False,
            server_default="medium",
        ),
        # Which motion first heard about this concern. Lets the CS-side
        # surface "Sales heard about pricing back in Feb" even when the
        # current interaction is on a different motion.
        sa.Column("source_motion", sa.String(length=32), nullable=True),
        sa.Column(
            "first_seen_interaction_id",
            uuid_t,
            sa.ForeignKey("interactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "last_seen_interaction_id",
            uuid_t,
            sa.ForeignKey("interactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Evidence trail: list of ``{interaction_id, quote, occurred_at,
        # sentiment}`` so the UI can show provenance for every concern
        # without re-running the analyzer.
        sa.Column("evidence", jsonb, nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'monitoring', 'resolved', 'dormant')",
            name="ck_customer_concerns_status",
        ),
        sa.CheckConstraint(
            "severity IN ('high', 'medium', 'low')",
            name="ck_customer_concerns_severity",
        ),
        sa.CheckConstraint(
            "source_motion IS NULL OR source_motion IN "
            "('sales', 'customer_service', 'it_support', 'generic')",
            name="ck_customer_concerns_source_motion",
        ),
        # One concern per (customer, topic) so a recurring mention
        # updates the existing row instead of fanning out duplicates.
        sa.UniqueConstraint(
            "tenant_id", "customer_id", "topic",
            name="uq_customer_concerns_customer_topic",
        ),
    )
    op.create_index(
        "ix_customer_concerns_customer_status",
        "customer_concerns",
        ["customer_id", "status"],
    )
    op.create_index(
        "ix_customer_concerns_tenant_status",
        "customer_concerns",
        ["tenant_id", "status"],
    )

    # ── customer_commitments (their side) ──────────────────────────────
    op.create_table(
        "customer_commitments",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            uuid_t,
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_interaction_id",
            uuid_t,
            sa.ForeignKey("interactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        # Verbatim quote when extractable. Powers the "they said X on
        # April 14th" provenance line on the UI.
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        # open: outstanding. met: they followed through. broken: due
        # date passed without follow-through. dismissed: rep
        # explicitly cleared (e.g. the commitment is no longer
        # relevant after a contract change).
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="open",
        ),
        sa.Column("met_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'met', 'broken', 'dismissed')",
            name="ck_customer_commitments_status",
        ),
    )
    op.create_index(
        "ix_customer_commitments_customer_status",
        "customer_commitments",
        ["customer_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_commitments_customer_status",
        table_name="customer_commitments",
    )
    op.drop_table("customer_commitments")
    op.drop_index(
        "ix_customer_concerns_tenant_status", table_name="customer_concerns"
    )
    op.drop_index(
        "ix_customer_concerns_customer_status", table_name="customer_concerns"
    )
    op.drop_table("customer_concerns")
