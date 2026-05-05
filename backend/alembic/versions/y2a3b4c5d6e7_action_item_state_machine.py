"""Phase 5 — action item state-machine fields.

Adds five optional columns to ``action_items`` to support the Phase 5
action-item rebuild:

- ``dismiss_reason`` — free-text reason captured when an item is dismissed;
  feeds the dismiss-reason learning loop (consistent dismiss patterns
  suppress similar suggestions in matching contexts).
- ``snoozed_until`` — wake date for snoozed items; null for non-snoozed.
- ``call_script`` — JSONB list of bullet points / talking points the rep
  can use on the next call. Sibling to ``email_draft``.
- ``completed_at`` — timestamp of transition into completed/done status.
- ``dismissed_at`` — timestamp of transition into dismissed/rejected status.

All columns are nullable. No data migration on existing rows. The status
field continues to accept the wider set of spellings handled by the API
layer's filter aliases (``pending``, ``in_progress``, ``open``, ``done``,
``completed``, ``snoozed``, ``dismissed``, ``rejected``); the new
timestamp columns are populated on transition by application code, not
back-filled.

Revision ID: y2a3b4c5d6e7
Revises: x1f2a3b4c5d6
Create Date: 2026-05-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "y2a3b4c5d6e7"
down_revision: Union[str, None] = "x1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "action_items",
        sa.Column("dismiss_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("call_script", JSONB(), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("action_items", "dismissed_at")
    op.drop_column("action_items", "completed_at")
    op.drop_column("action_items", "call_script")
    op.drop_column("action_items", "snoozed_until")
    op.drop_column("action_items", "dismiss_reason")
