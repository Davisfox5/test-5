"""CRM sync: integrations.provider_config + crm_sync_logs.

Revision ID: 5c9b1f3a7e82
Revises: 8b3a2e5c9d41
Create Date: 2026-04-19 06:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "5c9b1f3a7e82"
down_revision: Union[str, None] = "8b3a2e5c9d41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column(
            "provider_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "crm_sync_logs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("customers_upserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contacts_upserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("briefs_rebuilt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_crm_sync_logs_tenant_id", "crm_sync_logs", ["tenant_id"])
    op.create_index("ix_crm_sync_logs_started_at", "crm_sync_logs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_crm_sync_logs_started_at", table_name="crm_sync_logs")
    op.drop_index("ix_crm_sync_logs_tenant_id", table_name="crm_sync_logs")
    op.drop_table("crm_sync_logs")
    op.drop_column("integrations", "provider_config")
