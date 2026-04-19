"""Interaction outcome columns + customer_outcome_events table.

Revision ID: 6e2a9f0c4b18
Revises: 4c8e1d6a2f5b
Create Date: 2026-04-19 02:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6e2a9f0c4b18"
down_revision: Union[str, None] = "4c8e1d6a2f5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Interaction outcome columns.
    op.add_column("interactions", sa.Column("outcome_type", sa.String(), nullable=True))
    op.add_column("interactions", sa.Column("outcome_value", sa.Float(), nullable=True))
    op.add_column("interactions", sa.Column("outcome_confidence", sa.Float(), nullable=True))
    op.add_column("interactions", sa.Column("outcome_source", sa.String(), nullable=True))
    op.add_column("interactions", sa.Column("outcome_notes", sa.Text(), nullable=True))
    op.add_column(
        "interactions",
        sa.Column("outcome_captured_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_interactions_outcome_type", "interactions", ["outcome_type"]
    )

    # Customer-level lifecycle events.
    op.create_table(
        "customer_outcome_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("magnitude", sa.Float(), nullable=True),
        sa.Column("signal_strength", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_customer_outcome_events_tenant_id", "customer_outcome_events", ["tenant_id"]
    )
    op.create_index(
        "ix_customer_outcome_events_customer_id",
        "customer_outcome_events",
        ["customer_id"],
    )
    op.create_index(
        "ix_customer_outcome_events_event_type",
        "customer_outcome_events",
        ["event_type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_outcome_events_event_type", table_name="customer_outcome_events"
    )
    op.drop_index(
        "ix_customer_outcome_events_customer_id", table_name="customer_outcome_events"
    )
    op.drop_index(
        "ix_customer_outcome_events_tenant_id", table_name="customer_outcome_events"
    )
    op.drop_table("customer_outcome_events")

    op.drop_index("ix_interactions_outcome_type", table_name="interactions")
    op.drop_column("interactions", "outcome_captured_at")
    op.drop_column("interactions", "outcome_notes")
    op.drop_column("interactions", "outcome_source")
    op.drop_column("interactions", "outcome_confidence")
    op.drop_column("interactions", "outcome_value")
    op.drop_column("interactions", "outcome_type")
