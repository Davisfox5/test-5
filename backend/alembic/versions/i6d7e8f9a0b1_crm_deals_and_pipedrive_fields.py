"""add crm_deals table + crm_sync_logs.deals_upserted

Revision ID: i6d7e8f9a0b1
Revises: h5c6d7e8f9a0
Create Date: 2026-04-23

Adds the ``crm_deals`` table so the Pipedrive adapter (and future
HubSpot/Salesforce deal pulls) can persist a narrow projection of each
deal — enough to drive deal-aware coaching, write-back, and pipeline
reports without re-pulling on every request. The source of truth stays
in the CRM.

Also adds ``crm_sync_logs.deals_upserted`` so the admin UI can show
deal counts alongside customers/contacts.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "i6d7e8f9a0b1"
down_revision: Union[str, None] = "h5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "crm_sync_logs",
        sa.Column(
            "deals_upserted",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_table(
        "crm_deals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("stage", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("amount", sa.Float()),
        sa.Column("currency", sa.String()),
        sa.Column("probability", sa.Float()),
        sa.Column("close_date", sa.String()),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
        ),
        sa.Column("owner_name", sa.String()),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id", "provider", "external_id",
            name="uq_crm_deals_tenant_provider_external",
        ),
    )
    op.create_index("ix_crm_deals_tenant_id", "crm_deals", ["tenant_id"])
    op.create_index("ix_crm_deals_customer_id", "crm_deals", ["customer_id"])
    op.create_index("ix_crm_deals_contact_id", "crm_deals", ["contact_id"])


def downgrade() -> None:
    op.drop_index("ix_crm_deals_contact_id", table_name="crm_deals")
    op.drop_index("ix_crm_deals_customer_id", table_name="crm_deals")
    op.drop_index("ix_crm_deals_tenant_id", table_name="crm_deals")
    op.drop_table("crm_deals")
    op.drop_column("crm_sync_logs", "deals_upserted")
