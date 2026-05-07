"""Stream 4 — AudioHook session table.

Adds ``audiohook_sessions`` to back the
:class:`backend.app.models.AudiohookSession` ORM model. One row per
Genesys Cloud AudioHook conversation; sibling of ``live_sessions``
(NOT a foreign key — the AudioHook flow never produces a LiveSession
row, see the ORM docstring for why).

Revision ID: audiohook_001
Revises: z3b4c5d6e7f8
Create Date: 2026-05-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "audiohook_001"
down_revision: Union[str, None] = "z3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audiohook_sessions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("audiohook_session_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=True),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("participant_id", sa.String(), nullable=True),
        sa.Column(
            "channel",
            sa.String(),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column(
            "media_format",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "audio_frames_received",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "audio_bytes_received",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "is_consent_attested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.CheckConstraint(
            "channel IN ('agent', 'customer', 'both', 'unknown')",
            name="ck_audiohook_sessions_channel",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "audiohook_session_id",
            name="uq_audiohook_sessions_tenant_session",
        ),
    )
    op.create_index(
        "ix_audiohook_sessions_tenant_id",
        "audiohook_sessions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_audiohook_sessions_organization_id",
        "audiohook_sessions",
        ["organization_id"],
    )
    op.create_index(
        "ix_audiohook_sessions_conversation_id",
        "audiohook_sessions",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audiohook_sessions_conversation_id",
        table_name="audiohook_sessions",
    )
    op.drop_index(
        "ix_audiohook_sessions_organization_id",
        table_name="audiohook_sessions",
    )
    op.drop_index(
        "ix_audiohook_sessions_tenant_id",
        table_name="audiohook_sessions",
    )
    op.drop_table("audiohook_sessions")
