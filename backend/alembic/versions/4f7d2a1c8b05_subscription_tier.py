"""Add tenant.subscription_tier.

Revision ID: 4f7d2a1c8b05
Revises: 3e1b8f4a2c9d
Create Date: 2026-04-19 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4f7d2a1c8b05"
down_revision: Union[str, None] = "3e1b8f4a2c9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "subscription_tier",
            sa.String(),
            nullable=False,
            server_default="solo",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "subscription_tier")
