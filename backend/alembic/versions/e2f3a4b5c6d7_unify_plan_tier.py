"""Unify plan tier — drop redundant ``tenants.subscription_tier`` column.

The pre-consolidation schema carried two tier columns on ``tenants``:

* ``plan_tier`` (sandbox/starter/growth/enterprise) — customer-facing,
  consumed by ``/api/v1/me`` + the Next.js app.
* ``subscription_tier`` (solo/team/pro/enterprise) — seat/feature
  catalog driven by Stripe webhooks.

They were semantically overlapping. We've merged the two into the
unified :mod:`backend.app.plans` module. This migration:

1. Backfills ``plan_tier`` from ``subscription_tier`` for any tenant
   still on the default ``sandbox`` (so Stripe-set tiers win over the
   app default). Legacy ``solo|team|pro`` map to
   ``sandbox|starter|growth``.
2. Drops the ``subscription_tier`` column.

Revision ID: e2f3a4b5c6d7
Revises: d1f2a3b4c5e6
Create Date: 2026-04-21 06:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, None] = "d1f2a3b4c5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_TO_MODERN = {
    "solo": "sandbox",
    "team": "starter",
    "pro": "growth",
    "enterprise": "enterprise",
}


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Backfill plan_tier from subscription_tier where plan_tier is
    #    still the default 'sandbox' and subscription_tier carries a
    #    non-default value. Stripe-set tiers win over the app default.
    for legacy, modern in _LEGACY_TO_MODERN.items():
        conn.execute(
            sa.text(
                """
                UPDATE tenants
                   SET plan_tier = :modern
                 WHERE plan_tier = 'sandbox'
                   AND subscription_tier = :legacy
                """
            ),
            {"modern": modern, "legacy": legacy},
        )

    # 2. Drop the redundant column.
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("subscription_tier")


def downgrade() -> None:
    # Re-add the column with the old default; data is not recovered.
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(
            sa.Column(
                "subscription_tier",
                sa.String(),
                nullable=False,
                server_default="solo",
            )
        )
