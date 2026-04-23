"""three-tier plans: plan_tier + trial_ends_at on tenants, demo_email_captures table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-19 22:15:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "plan_tier",
            sa.String(),
            nullable=False,
            server_default="sandbox",
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "demo_email_captures",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("utm", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("converted_tenant_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["converted_tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_demo_email_captures_email", "demo_email_captures", ["email"])
    op.create_index("ix_demo_email_captures_created_at", "demo_email_captures", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_demo_email_captures_created_at", table_name="demo_email_captures")
    op.drop_index("ix_demo_email_captures_email", table_name="demo_email_captures")
    op.drop_table("demo_email_captures")
    op.drop_column("tenants", "trial_ends_at")
    op.drop_column("tenants", "plan_tier")
