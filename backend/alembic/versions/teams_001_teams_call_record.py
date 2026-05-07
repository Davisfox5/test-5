"""Stream 3 — Microsoft Teams compliance recording: ``teams_call_records``.

Adds the single control-plane table for Teams compliance recording. The
table is empty in the scaffold round — the .NET media bot that will
eventually populate it is out of scope. Landing the migration now means
the bot follow-on workstream doesn't have to coordinate a schema change
with whatever else is in flight at that time.

Schema (mirrors :class:`TeamsCallRecord` in models.py):

* ``id`` — uuid primary key.
* ``tenant_id`` — FK to ``tenants(id)`` with cascade delete.
* ``call_id`` — Microsoft's call identifier (string, not UUID — Graph
  occasionally emits non-GUID identifiers for legacy meetings).
* ``organizer`` — UPN string, nullable.
* ``participants`` — JSONB array, default ``[]``.
* ``join_url`` / ``recording_url`` — text, nullable.
* ``certification_status`` — enum-by-CHECK
  (``scaffold`` | ``bot_required`` | ``recording_fetched``).
* ``created_at`` / ``updated_at`` — timestamptz, server defaults.

Constraints:

* Unique on ``(tenant_id, call_id)`` — a tenant cannot have two records
  for the same call.
* Check on ``certification_status`` for the three legal values.

Index on ``tenant_id`` for the dominant scan pattern (admin UI lists
calls within a tenant).

Revision ID: teams_001_teams_call_record
Revises: z3b4c5d6e7f8
Create Date: 2026-05-07
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "teams_001_teams_call_record"
down_revision: Union[str, None] = "z3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "teams_call_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("call_id", sa.String(), nullable=False),
        sa.Column("organizer", sa.String(), nullable=True),
        sa.Column(
            "participants",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("join_url", sa.Text(), nullable=True),
        sa.Column("recording_url", sa.Text(), nullable=True),
        sa.Column(
            "certification_status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'scaffold'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "call_id", name="uq_teams_call_records_tenant_call"
        ),
        sa.CheckConstraint(
            "certification_status IN ('scaffold','bot_required','recording_fetched')",
            name="ck_teams_call_records_certification_status",
        ),
    )
    op.create_index(
        "ix_teams_call_records_tenant_id",
        "teams_call_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_teams_call_records_call_id",
        "teams_call_records",
        ["call_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_teams_call_records_call_id", table_name="teams_call_records")
    op.drop_index("ix_teams_call_records_tenant_id", table_name="teams_call_records")
    op.drop_table("teams_call_records")
