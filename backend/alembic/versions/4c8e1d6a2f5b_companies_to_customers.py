"""Rename companies -> customers; contacts.company_id -> customer_id.

The table was named `companies` in the initial schema but semantically
represents the tenant's *own customers* (CRM-style). We rename to
``customers`` for clarity and to match the split between the selling entity
(``Tenant``) and the entities it sells to (``Customer``). Also adds the new
``customers.customer_brief`` JSONB column used by the CustomerBriefBuilder.

Revision ID: 4c8e1d6a2f5b
Revises: 9b4d7e1a2c3f
Create Date: 2026-04-19 01:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "4c8e1d6a2f5b"
down_revision: Union[str, None] = "9b4d7e1a2c3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("companies", "customers")
    op.alter_column("contacts", "company_id", new_column_name="customer_id")
    op.add_column(
        "customers",
        sa.Column(
            "customer_brief",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("customers", "customer_brief")
    op.alter_column("contacts", "customer_id", new_column_name="company_id")
    op.rename_table("customers", "companies")
