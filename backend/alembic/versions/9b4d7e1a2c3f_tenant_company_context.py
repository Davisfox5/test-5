"""Add Tenant.company_context JSONB for LINDA's per-tenant context brief.

Revision ID: 9b4d7e1a2c3f
Revises: 7a1c3e4b9d12
Create Date: 2026-04-19 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "9b4d7e1a2c3f"
down_revision: Union[str, None] = "7a1c3e4b9d12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "company_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "company_context")
