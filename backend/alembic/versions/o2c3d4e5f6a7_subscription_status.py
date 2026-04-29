"""Add ``tenants.subscription_status``.

Lifecycle marker for the trial-expiry sweep + Stripe webhook handling
("active" | "expired" | "past_due"). Backfilled from existing
``trial_ends_at`` / ``stripe_subscription_id`` heuristics.

Revision ID: o2c3d4e5f6a7
Revises: n1b2c3d4e5f6
Create Date: 2026-04-28 23:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "o2c3d4e5f6a7"
down_revision: Union[str, None] = "n1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "subscription_status",
            sa.String(),
            nullable=False,
            server_default="active",
        ),
    )
    # Best-effort backfill: any sandbox tenant whose trial has already
    # ended is marked "expired" so the trial-expiry sweep doesn't have
    # to re-emit the lifecycle event for known-stale rows.
    op.execute(
        "UPDATE tenants SET subscription_status = 'expired' "
        "WHERE plan_tier = 'sandbox' AND trial_ends_at IS NOT NULL "
        "AND trial_ends_at < NOW()"
    )


def downgrade() -> None:
    op.drop_column("tenants", "subscription_status")
