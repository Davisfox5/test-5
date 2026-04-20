"""Customer notes + tenant feature flag defaults.

Revision ID: 8b3a2e5c9d41
Revises: 2f8c1b5d7a43
Create Date: 2026-04-19 05:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8b3a2e5c9d41"
down_revision: Union[str, None] = "2f8c1b5d7a43"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customer_notes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=True),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"]),
    )
    op.create_index("ix_customer_notes_tenant_id", "customer_notes", ["tenant_id"])
    op.create_index("ix_customer_notes_customer_id", "customer_notes", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_customer_notes_customer_id", table_name="customer_notes")
    op.drop_index("ix_customer_notes_tenant_id", table_name="customer_notes")
    op.drop_table("customer_notes")
