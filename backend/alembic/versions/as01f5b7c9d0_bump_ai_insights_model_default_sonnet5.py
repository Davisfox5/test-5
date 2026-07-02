"""bump ai_insights.model column default to claude-sonnet-5

The ``ai_insights.model`` column carried a server_default of
``claude-sonnet-4-6`` from the initial schema. The application writes the model
explicitly on every insert, so this default is a fallback only — but we keep it
current with the tier catalog (Sonnet bumped 4-6 -> 5) so a row inserted without
an explicit model records the model we'd actually use.

Revision ID: as01f5b7c9d0
Revises: ap_003_draft_state
Create Date: 2026-07-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "as01f5b7c9d0"
down_revision = "ap_003_draft_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ai_insights",
        "model",
        existing_type=sa.VARCHAR(length=100),
        server_default=sa.text("'claude-sonnet-5'::character varying"),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "ai_insights",
        "model",
        existing_type=sa.VARCHAR(length=100),
        server_default=sa.text("'claude-sonnet-4-6'::character varying"),
        existing_nullable=True,
    )
