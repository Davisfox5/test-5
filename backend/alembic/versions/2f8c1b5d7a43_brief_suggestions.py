"""Tenant brief suggestions from the Infer-From-Sources agent.

Revision ID: 2f8c1b5d7a43
Revises: 1d5f8a0c3e97
Create Date: 2026-04-19 04:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "2f8c1b5d7a43"
down_revision: Union[str, None] = "1d5f8a0c3e97"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_brief_suggestions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("section", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column(
            "proposed_value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
    )
    op.create_index(
        "ix_tenant_brief_suggestions_tenant_id",
        "tenant_brief_suggestions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_tenant_brief_suggestions_status",
        "tenant_brief_suggestions",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_brief_suggestions_status", table_name="tenant_brief_suggestions"
    )
    op.drop_index(
        "ix_tenant_brief_suggestions_tenant_id",
        table_name="tenant_brief_suggestions",
    )
    op.drop_table("tenant_brief_suggestions")
