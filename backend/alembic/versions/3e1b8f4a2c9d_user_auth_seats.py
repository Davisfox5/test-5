"""Add per-user login + tenant seat limits.

- ``users``: ``password_hash``, ``is_active``, ``last_login_at``.
- ``tenants``: ``seat_limit``, ``admin_seat_limit``.

Backfill: every existing user becomes ``role='admin'`` on this migration
so no tenant gets locked out when we start enforcing admin-only
endpoints. New users default to ``agent`` from the ORM.

Revision ID: 3e1b8f4a2c9d
Revises: 7d4a6e1b9c38
Create Date: 2026-04-19 08:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3e1b8f4a2c9d"
down_revision: Union[str, None] = "7d4a6e1b9c38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "tenants",
        sa.Column(
            "seat_limit",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "admin_seat_limit",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    # Grandfather every existing user as an admin so enforcement of
    # admin-only endpoints doesn't lock anyone out mid-rollout.
    op.execute("UPDATE users SET role = 'admin' WHERE role IS NULL OR role = ''")
    op.execute("UPDATE users SET role = 'admin'")


def downgrade() -> None:
    op.drop_column("tenants", "admin_seat_limit")
    op.drop_column("tenants", "seat_limit")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "is_active")
    op.drop_column("users", "password_hash")
