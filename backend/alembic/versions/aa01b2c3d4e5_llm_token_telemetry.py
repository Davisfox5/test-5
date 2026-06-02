"""LLM token telemetry — adaptive max_tokens ceiling.

Two tables that let us replace trial-and-error static ``max_tokens`` tuning
with measured ceilings:

1. ``llm_call_telemetry`` — append-only log of every Anthropic completion's
   token usage (input, output, cache reads, stop_reason, request cap). One
   row per call, kept ~30 days then rolled up.

2. ``llm_ceiling_recommendation`` — singleton row per (call_site, tier)
   with rolling percentiles + a recommended ceiling. A daily Celery task
   recomputes these once a (call_site, tier) has ≥200 samples or 14 days
   of history. ``compute_max_tokens`` consults this table at request time
   (with an in-process cache); a recommendation set to ``p99 * 1.2`` keeps
   real output within budget while flagging truncation if the model wants
   more.

The model's billing is on actual output tokens, not the cap, so this
isn't primarily a cost saver — it's a quality + observability tool that
catches silent truncation and removes the need to guess ceilings.

Revision ID: aa01b2c3d4e5
Revises: z3b4c5d6e7f8
Create Date: 2026-06-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "aa01b2c3d4e5"
down_revision: Union[str, None] = "z3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_call_telemetry",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("call_site", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("request_max_tokens", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cache_read_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cache_creation_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_llm_telemetry_site_tier_created",
        "llm_call_telemetry",
        ["call_site", "tier", "created_at"],
    )
    op.create_index(
        "ix_llm_telemetry_created",
        "llm_call_telemetry",
        ["created_at"],
    )

    op.create_table(
        "llm_ceiling_recommendation",
        sa.Column("call_site", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("p50", sa.Integer(), nullable=False),
        sa.Column("p95", sa.Integer(), nullable=False),
        sa.Column("p99", sa.Integer(), nullable=False),
        sa.Column("max_observed", sa.Integer(), nullable=False),
        sa.Column("truncation_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("recommended_ceiling", sa.Integer(), nullable=False),
        sa.Column(
            "window_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "window_end", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("call_site", "tier"),
    )


def downgrade() -> None:
    op.drop_table("llm_ceiling_recommendation")
    op.drop_index("ix_llm_telemetry_created", table_name="llm_call_telemetry")
    op.drop_index("ix_llm_telemetry_site_tier_created", table_name="llm_call_telemetry")
    op.drop_table("llm_call_telemetry")
