"""Feedback-event daily rollup table (retention aid).

Enables the retention sweep to delete old raw ``feedback_events`` rows
without losing the aggregate signal that calibration + prompt-variant
winner selection care about. After the sweep runs, raw rows older than
``FEEDBACK_EVENT_RAW_RETENTION_DAYS`` are dropped, but the per-day
per-(surface, event_type) counts live on in this table indefinitely.

``webhook_deliveries`` has no rollup — audits read the full row. The
retention sweep uses a longer window (see tasks.py).

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-04-21 08:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feedback_daily_rollup",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "day", "surface", "event_type"),
    )
    op.create_index(
        "ix_feedback_daily_rollup_tenant_day",
        "feedback_daily_rollup",
        ["tenant_id", "day"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feedback_daily_rollup_tenant_day", table_name="feedback_daily_rollup"
    )
    op.drop_table("feedback_daily_rollup")
