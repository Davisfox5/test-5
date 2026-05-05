"""Phase 0 — telemetry foundation.

Two changes that unblock outcome-calibrated learning loops (Phase 4
classifier, dismiss-reason learning loop, Change-Readiness calibration):

1. ``interaction_features`` gains three columns capturing what produced
   the analysis: ``analysis_prompt_version``, ``triage_prompt_version``,
   ``model_used``. Outcome data joined against analyses-by-prompt-version
   is what lets us tell "did the v2 prompt actually predict churn better
   than v1?" without ambiguity. Nullable because back-fill is impossible.

2. New ``intervention_events`` table — append-only log of rep / manager /
   system actions that affect a customer's outcome (follow-up sent,
   manager review, escalation, action-item lifecycle transitions, discount
   offered, etc.). Required for bias correction at training time: a
   customer who churns after we flagged them high-risk and intervened is
   a different signal than one who churns after we flagged them and did
   nothing.

No data migration. Existing ``interaction_features`` rows are untouched;
new pipeline runs populate the version columns going forward. Outcome
event ingestion (``customer_outcome_events``) and idempotency
infrastructure already exist — this migration only adds the missing
intervention side.

Revision ID: z3b4c5d6e7f8
Revises: y2a3b4c5d6e7
Create Date: 2026-05-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "z3b4c5d6e7f8"
down_revision: Union[str, None] = "y2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INTERVENTION_KINDS = (
    "follow_up_sent",
    "manager_review",
    "escalation",
    "rep_callback",
    "discount_offered",
    "action_item_completed",
    "action_item_dismissed",
    "action_item_snoozed",
    "action_item_reopened",
    "scorecard_review_completed",
    "other",
)


def upgrade() -> None:
    # ── interaction_features version columns ─────────────────────────
    op.add_column(
        "interaction_features",
        sa.Column("analysis_prompt_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "interaction_features",
        sa.Column("triage_prompt_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "interaction_features",
        sa.Column("model_used", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_interaction_features_analysis_prompt_version",
        "interaction_features",
        ["analysis_prompt_version"],
    )

    # ── intervention_events table ────────────────────────────────────
    op.create_table(
        "intervention_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interaction_id", UUID(as_uuid=True), sa.ForeignKey("interactions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("customer_id", UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=True),
        sa.Column("actor_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "kind",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("meta", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "kind IN ("
            + ", ".join(f"'{k}'" for k in _INTERVENTION_KINDS)
            + ")",
            name="ck_intervention_events_kind",
        ),
    )
    op.create_index(
        "ix_intervention_events_tenant_customer",
        "intervention_events",
        ["tenant_id", "customer_id"],
    )
    op.create_index(
        "ix_intervention_events_interaction_id",
        "intervention_events",
        ["interaction_id"],
    )
    op.create_index(
        "ix_intervention_events_occurred_at",
        "intervention_events",
        ["occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_intervention_events_occurred_at", table_name="intervention_events")
    op.drop_index("ix_intervention_events_interaction_id", table_name="intervention_events")
    op.drop_index("ix_intervention_events_tenant_customer", table_name="intervention_events")
    op.drop_table("intervention_events")
    op.drop_index(
        "ix_interaction_features_analysis_prompt_version",
        table_name="interaction_features",
    )
    op.drop_column("interaction_features", "model_used")
    op.drop_column("interaction_features", "triage_prompt_version")
    op.drop_column("interaction_features", "analysis_prompt_version")
