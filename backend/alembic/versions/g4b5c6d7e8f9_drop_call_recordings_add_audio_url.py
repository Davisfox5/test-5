"""drop call_recordings + recording retention; add interactions.audio_url

Revision ID: g4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-04-22

Drops:
* ``call_recordings`` table (LINDA no longer stores call audio —
  recordings come in via ``/interactions/ingest-recording`` and are
  transcribed-then-discarded).
* ``tenants.audio_storage_enabled``
* ``tenants.recording_retention_days``

Adds:
* ``interactions.audio_url`` — pointer to a provider-hosted recording
  URL (MiaRec / Dubber / Teams / MetaSwitch). When set, the Celery
  worker passes the URL to Deepgram directly; bytes never land on our
  storage.

Downgrade re-creates ``call_recordings`` and the two tenant columns
for operational rollback; existing rows are not restored.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g4b5c6d7e8f9"
down_revision: Union[str, None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop indexes before the table (Postgres tolerates either, but
    # being explicit avoids warnings).
    op.drop_index(
        "ix_call_recordings_interaction_id", table_name="call_recordings"
    )
    op.drop_index("ix_call_recordings_tenant_id", table_name="call_recordings")
    op.drop_table("call_recordings")

    op.drop_column("tenants", "audio_storage_enabled")
    op.drop_column("tenants", "recording_retention_days")

    op.add_column(
        "interactions",
        sa.Column("audio_url", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("interactions", "audio_url")

    op.add_column(
        "tenants",
        sa.Column(
            "recording_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "audio_storage_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_table(
        "call_recordings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "interaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interactions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "live_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("live_sessions.id", ondelete="SET NULL"),
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_recording_id", sa.String()),
        sa.Column("s3_key", sa.String()),
        sa.Column("content_type", sa.String(), nullable=False, server_default="audio/wav"),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("stored_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_call_recordings_tenant_id", "call_recordings", ["tenant_id"]
    )
    op.create_index(
        "ix_call_recordings_interaction_id", "call_recordings", ["interaction_id"]
    )
