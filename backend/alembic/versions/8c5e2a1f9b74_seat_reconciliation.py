"""Seat-reconciliation columns.

Revision ID: 8c5e2a1f9b74
Revises: 7e3b9d8c5a41
Create Date: 2026-04-19 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c5e2a1f9b74"
down_revision: Union[str, None] = "7e3b9d8c5a41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("suspension_reason", sa.String(), nullable=True))
    op.add_column(
        "tenants",
        sa.Column(
            "pending_seat_reconciliation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "pending_seat_reconciliation")
    op.drop_column("users", "suspension_reason")
