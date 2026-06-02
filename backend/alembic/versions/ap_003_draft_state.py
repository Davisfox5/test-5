"""action_steps.draft_state for lazy artifact generation.

Adds a per-step ``draft_state`` column distinguishing:
- ``drafted``         — artifact body has been generated (default; back-
                        compat for existing plans)
- ``ready_to_draft``  — all critical input slots filled; Call C is
                        firing or about to fire
- ``pending_upstream``— at least one critical slot is unfilled and
                        depends on an upstream step
- ``draft_blocked``   — an upstream step that was the source of a
                        critical slot got skipped or deleted; rep
                        must intervene

Backs the redesign that stops pre-drafting steps whose critical inputs
aren't ready. See backend/app/services/action_plan/synthesizer.py
for the classification logic and backend/app/services/action_plan/
engine.py for the on-completion fire hook.

Revision ID: ap_003_draft_state
Revises: ad04e5f6a7b8
Create Date: 2026-06-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "ap_003_draft_state"
down_revision: Union[str, None] = "ad04e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "action_steps",
        sa.Column(
            "draft_state",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'drafted'"),
        ),
    )
    # Light index — UI listings filter on draft_state for the per-step
    # rendering switch.
    op.create_index(
        "ix_action_steps_draft_state",
        "action_steps",
        ["plan_id", "draft_state"],
    )


def downgrade() -> None:
    op.drop_index("ix_action_steps_draft_state", table_name="action_steps")
    op.drop_column("action_steps", "draft_state")
