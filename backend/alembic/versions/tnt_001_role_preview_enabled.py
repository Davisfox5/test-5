"""Add ``tenants.role_preview_enabled`` for non-sandbox role-preview opt-in.

The role-preview pill was originally sandbox-tier-only — the assumption
being that paid tenants have multiple seated users and don't need a
fake-toggle. That broke once we needed the pill on an internal /
demo tenant running on the enterprise tier (and not willing to be
demoted to sandbox just to flip between views). This column is an
explicit per-tenant escape hatch: when True, the principal resolver
and ``POST /me/preview-role`` treat the tenant like a sandbox tenant
for role-preview purposes only — no other feature gating changes.

Schema notes:

* Boolean, NOT NULL, default False. Existing rows backfill to False so
  no real customer suddenly sees the pill.
* The sandbox tier still honours the pill regardless of this flag — the
  gate is ``plan_tier = 'sandbox' OR role_preview_enabled = True``.
* No index. Lookup is always by tenant id (FK on every request) so the
  flag is read from the row that's already in scope.

Revision ID: tnt_001_role_preview_enabled
Revises: c5e6d7f8a9b0
Create Date: 2026-05-16
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "tnt_001_role_preview_enabled"
down_revision: Union[str, None] = "c5e6d7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "role_preview_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "role_preview_enabled")
