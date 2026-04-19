"""Tenant Stripe linkage.

Revision ID: 7e3b9d8c5a41
Revises: 6b4c1e9a2d73
Create Date: 2026-04-19 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7e3b9d8c5a41"
down_revision: Union[str, None] = "6b4c1e9a2d73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("stripe_customer_id", sa.String(), nullable=True))
    op.add_column(
        "tenants", sa.Column("stripe_subscription_id", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_tenants_stripe_customer_id", "tenants", ["stripe_customer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_tenants_stripe_customer_id", table_name="tenants")
    op.drop_column("tenants", "stripe_subscription_id")
    op.drop_column("tenants", "stripe_customer_id")
