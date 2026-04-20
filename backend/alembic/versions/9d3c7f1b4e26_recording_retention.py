"""Recording retention setting on Tenant.

Revision ID: 9d3c7f1b4e26
Revises: 8c5e2a1f9b74
Create Date: 2026-04-19 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9d3c7f1b4e26"
down_revision: Union[str, None] = "8c5e2a1f9b74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "recording_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "recording_retention_days")
