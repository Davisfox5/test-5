"""Add per-tenant retention overrides + audio default.

Adds ``retention_days_webhook_deliveries`` and
``retention_days_feedback_events`` to ``tenants`` (NULL ⇒ system default).
Bumps the default for ``audio_retention_hours`` to 168 (7 days) — the
prior 24h default was set when audio storage was a debugging convenience
rather than a customer expectation. Existing tenants keep their value.

Revision ID: p3d4e5f6a7b8
Revises: o2c3d4e5f6a7
Create Date: 2026-04-28 23:55:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "p3d4e5f6a7b8"
down_revision: Union[str, None] = "o2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "retention_days_webhook_deliveries",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "retention_days_feedback_events",
            sa.Integer(),
            nullable=True,
        ),
    )
    # Move the audio default to 7 days for new tenants. Existing rows
    # keep whatever they currently hold (24 if untouched).
    op.alter_column(
        "tenants",
        "audio_retention_hours",
        server_default="168",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "tenants",
        "audio_retention_hours",
        server_default="24",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.drop_column("tenants", "retention_days_feedback_events")
    op.drop_column("tenants", "retention_days_webhook_deliveries")
