"""outcomes hardening — idempotency, HMAC, dead-letter, tenant config

Revision ID: c9e1d4b7f3a8
Revises: b58a9f3c2e11
Create Date: 2026-04-17 14:30:00.000000

Adds the pieces needed to take ``POST /outcomes`` from "tenant-scoped
webhook" to "production integration surface":

- ``outcome_event_ingestions`` — idempotency table keyed on
  (tenant_id, event_id).  Repeated webhook deliveries with the same
  ``event_id`` are 200-accepted but not double-applied.
- ``dropped_outcome_events`` — dead-letter log for payloads that fail
  semantic validation, so integrators can debug without losing data.
- ``Tenant.outcomes_hmac_secret`` — per-tenant shared secret for
  verifying ``X-Linda-Signature`` headers.
- ``Tenant.audio_retention_hours`` — controls how long processed
  audio is retained.  Ships with a default of 24h so the S3 lifecycle
  rule's grace period is never zero.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers
revision: str = "c9e1d4b7f3a8"
down_revision: Union[str, None] = "b58a9f3c2e11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("outcomes_hmac_secret", sa.String(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "audio_retention_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
    )

    op.create_table(
        "outcome_event_ingestions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("outcome_type", sa.String(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "event_id", name="uq_outcome_ingestion_event"
        ),
    )

    op.create_table(
        "dropped_outcome_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("headers_snapshot", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dropped_outcome_events_tenant_time",
        "dropped_outcome_events",
        ["tenant_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dropped_outcome_events_tenant_time",
        table_name="dropped_outcome_events",
    )
    op.drop_table("dropped_outcome_events")
    op.drop_table("outcome_event_ingestions")
    op.drop_column("tenants", "audio_retention_hours")
    op.drop_column("tenants", "outcomes_hmac_secret")
