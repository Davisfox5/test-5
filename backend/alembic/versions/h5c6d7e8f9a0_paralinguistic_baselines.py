"""add tenants.paralinguistic_baselines

Revision ID: h5c6d7e8f9a0
Revises: g4b5c6d7e8f9
Create Date: 2026-04-22

Adds a JSONB column to ``tenants`` for per-tenant acoustic baselines
(customer intensity p90, agent pitch σ p50, etc.). Populated by the
nightly orchestrator; consumed by the churn + sentiment scorers to
translate raw per-call paralinguistic metrics into "hotter than
normal" / "flatter than normal" signals.

Defaults to ``'{}'`` so rows inserted before the baseline job runs
don't break anything downstream.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "h5c6d7e8f9a0"
down_revision: Union[str, None] = "g4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "paralinguistic_baselines",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "paralinguistic_baselines")
