"""action_step.awaits_response + step_feedback_logs table.

Two related changes:

1. ``action_steps.awaits_response`` (bool, default false). Set by the
   synthesizer per step. When True, the engine holds the step in
   ``awaiting_response`` after the rep clicks Send instead of jumping
   to ``done``. Lets downstream steps stay correctly blocked when an
   outbound email genuinely needs a reply.

2. ``step_feedback_logs`` table. Per-user record of step edits so the
   synthesizer can adapt FUTURE plans toward this user's preferred
   phrasing/channel/priority. User-scoped, not tenant-scoped, so one
   rep's stylistic choices don't silently override a teammate's.

Revision ID: ap_002_step_feedback_and_awaits
Revises: aa01b2c3d4e5
Create Date: 2026-05-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "ap_002_step_feedback_and_awaits"
down_revision: Union[str, None] = "aa01b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "action_steps",
        sa.Column(
            "awaits_response",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_table(
        "step_feedback_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("action_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("action_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("before", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("after", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("changed_keys", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_step_feedback_logs_user_created",
        "step_feedback_logs",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_step_feedback_logs_user_created",
        table_name="step_feedback_logs",
    )
    op.drop_table("step_feedback_logs")
    op.drop_column("action_steps", "awaits_response")
