"""Onboarding interview sessions.

Revision ID: 1d5f8a0c3e97
Revises: 6e2a9f0c4b18
Create Date: 2026-04-19 03:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "1d5f8a0c3e97"
down_revision: Union[str, None] = "6e2a9f0c4b18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "onboarding_sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("started_by_user_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"]),
    )
    op.create_index(
        "ix_onboarding_sessions_tenant_id", "onboarding_sessions", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_onboarding_sessions_tenant_id", table_name="onboarding_sessions"
    )
    op.drop_table("onboarding_sessions")
