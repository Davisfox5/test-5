"""Call recordings table.

Revision ID: 6b4c1e9a2d73
Revises: 9a5c2e7d4f18
Create Date: 2026-04-19 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6b4c1e9a2d73"
down_revision: Union[str, None] = "9a5c2e7d4f18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "call_recordings",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=True),
        sa.Column("live_session_id", sa.UUID(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_recording_id", sa.String(), nullable=True),
        sa.Column("s3_key", sa.String(), nullable=True),
        sa.Column(
            "content_type", sa.String(), nullable=False, server_default="audio/wav"
        ),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("stored_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["live_session_id"], ["live_sessions.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_call_recordings_tenant_id", "call_recordings", ["tenant_id"])
    op.create_index(
        "ix_call_recordings_interaction_id", "call_recordings", ["interaction_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_call_recordings_interaction_id", table_name="call_recordings"
    )
    op.drop_index("ix_call_recordings_tenant_id", table_name="call_recordings")
    op.drop_table("call_recordings")
