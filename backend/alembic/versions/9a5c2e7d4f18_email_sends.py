"""Outbound email sends table.

Revision ID: 9a5c2e7d4f18
Revises: 4f7d2a1c8b05
Create Date: 2026-04-19 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a5c2e7d4f18"
down_revision: Union[str, None] = "4f7d2a1c8b05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_sends",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=True),
        sa.Column("sender_user_id", sa.UUID(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("to_address", sa.String(), nullable=False),
        sa.Column("cc_address", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["sender_user_id"], ["users.id"]),
    )
    op.create_index("ix_email_sends_tenant_id", "email_sends", ["tenant_id"])
    op.create_index("ix_email_sends_interaction_id", "email_sends", ["interaction_id"])


def downgrade() -> None:
    op.drop_index("ix_email_sends_interaction_id", table_name="email_sends")
    op.drop_index("ix_email_sends_tenant_id", table_name="email_sends")
    op.drop_table("email_sends")
