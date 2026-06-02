"""Track Gmail watch / Graph subscription expiry on EmailSyncCursor.

The 12-hourly ``email_push_renew_subscriptions`` task previously re-issued
every Gmail watch and Graph subscription unconditionally — Google /
Microsoft happily accept the re-registration but it's two API calls per
integration per 12 h with no value when the existing subscription is
nowhere near expiry. With this column we only re-register when the prior
subscription is within 24 h of expiring.

The same column lets ``email_ingest_poll`` (the 15-min safety-net poll)
skip integrations whose push subscription is still healthy — the audit
flagged this as a ~5,760 wasted poll cycles/day leak.

Revision ID: ad04e5f6a7b8
Revises: ac03d4e5f6a7
Create Date: 2026-06-02
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "ad04e5f6a7b8"
down_revision: Union[str, None] = "ac03d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_sync_cursors",
        sa.Column(
            "push_subscription_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("email_sync_cursors", "push_subscription_expires_at")
