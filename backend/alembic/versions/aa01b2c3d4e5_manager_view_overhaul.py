"""Manager-view overhaul — anomaly alerts, recommendations, Slack OAuth, coaching notes.

Five new tables that back the 10,000-foot manager dashboard:

* ``manager_alerts`` — append-only feed of detected anomalies (topic spike,
  sentiment drop, churn surge, methodology drop). Deduped per active
  fingerprint so the same recurring spike doesn't re-fire until resolved.

* ``manager_recommendations`` — proactive next-move queue. One row per
  Haiku-drafted recommendation with a category that maps to a concrete
  artifact (coaching note, draft campaign, outreach action item, playbook
  entry) on apply.

* ``alert_channel_config`` — one row per tenant carrying threshold
  overrides + which channels (in-app, Slack) get severity-gated.

* ``coaching_notes`` — lightweight manager-to-rep memo store. Kept
  separate from ``action_items`` because that table requires an
  ``interaction_id`` and a manager-level memo isn't anchored to one call.

* ``slack_integration`` — per-tenant Slack OAuth install. Stores the
  encrypted bot token + the channel chosen for alert delivery.

Revision ID: aa01b2c3d4e5
Revises: z3b4c5d6e7f8
Create Date: 2026-05-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "aa01b2c3d4e5"
down_revision: Union[str, None] = "z3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ALERT_KINDS = ("topic_spike", "sentiment_drop", "churn_surge", "methodology_drop")

# Updated notifications.kind CHECK: extends the Phase 5B-6 vocabulary
# with ``manager_alert`` so the fanout layer can insert per-user
# notification rows when an anomaly fires.
_NOTIFICATION_KINDS = (
    "action_item_assigned",
    "action_item_comment",
    "action_item_returned",
    "action_item_due_soon",
    "action_item_overdue",
    "manager_review_completed",
    "scorecard_review_assigned",
    "manager_alert",
    "system",
    "other",
)
_ALERT_SEVERITIES = ("high", "medium", "low")
_REC_CATEGORIES = (
    "coach_rep",
    "run_campaign",
    "outreach_at_risk_customer",
    "promote_winning_script",
)
_REC_STATUSES = ("open", "applied", "dismissed", "expired")
_NOTE_STATUSES = ("open", "done", "dismissed")
_SEVERITY_THRESHOLDS = ("high", "medium", "low")


def upgrade() -> None:
    # ── extend notifications.kind CHECK to include 'manager_alert' ────
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        "kind IN (" + ", ".join(f"'{k}'" for k in _NOTIFICATION_KINDS) + ")",
    )

    # ── manager_alerts ───────────────────────────────────────────────
    op.create_table(
        "manager_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manager_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismiss_reason", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in _ALERT_KINDS) + ")",
            name="ck_manager_alerts_kind",
        ),
        sa.CheckConstraint(
            "severity IN (" + ", ".join(f"'{s}'" for s in _ALERT_SEVERITIES) + ")",
            name="ck_manager_alerts_severity",
        ),
    )
    op.create_index(
        "ix_manager_alerts_tenant_open",
        "manager_alerts",
        ["tenant_id", "acknowledged_at", "dismissed_at", "opened_at"],
    )
    # Partial unique: only ONE active fingerprint per tenant. A resolved
    # row frees the fingerprint for re-firing.
    op.create_index(
        "ux_manager_alerts_active_fingerprint",
        "manager_alerts",
        ["tenant_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    # ── manager_recommendations ──────────────────────────────────────
    op.create_table(
        "manager_recommendations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("manager_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("category", sa.String(length=48), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("target", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("score", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'open'")),
        sa.Column("applied_artifact_type", sa.String(length=48), nullable=True),
        sa.Column("applied_artifact_id", UUID(as_uuid=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismiss_reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "category IN (" + ", ".join(f"'{c}'" for c in _REC_CATEGORIES) + ")",
            name="ck_manager_recommendations_category",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _REC_STATUSES) + ")",
            name="ck_manager_recommendations_status",
        ),
    )
    op.create_index(
        "ix_manager_recommendations_tenant_open",
        "manager_recommendations",
        ["tenant_id", "status", "score"],
    )

    # ── alert_channel_config ─────────────────────────────────────────
    op.create_table(
        "alert_channel_config",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("inapp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("slack_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("slack_min_severity", sa.String(length=16), nullable=False, server_default=sa.text("'medium'")),
        sa.Column("topic_spike_pct_change_threshold", sa.Integer(), nullable=True),
        sa.Column("topic_spike_min_volume", sa.Integer(), nullable=True),
        sa.Column("sentiment_drop_threshold", sa.Numeric(4, 2), nullable=True),
        sa.Column("churn_surge_multiplier", sa.Numeric(4, 1), nullable=True),
        sa.Column("methodology_drop_threshold", sa.Numeric(4, 2), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "slack_min_severity IN (" + ", ".join(f"'{s}'" for s in _SEVERITY_THRESHOLDS) + ")",
            name="ck_alert_channel_config_min_severity",
        ),
    )

    # ── coaching_notes ───────────────────────────────────────────────
    op.create_table(
        "coaching_notes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assigned_to", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source_recommendation_id", UUID(as_uuid=True), sa.ForeignKey("manager_recommendations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'open'")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _NOTE_STATUSES) + ")",
            name="ck_coaching_notes_status",
        ),
    )
    op.create_index(
        "ix_coaching_notes_assigned",
        "coaching_notes",
        ["assigned_to", "status", "created_at"],
    )
    op.create_index(
        "ix_coaching_notes_tenant",
        "coaching_notes",
        ["tenant_id", "status"],
    )

    # ── slack_integration ────────────────────────────────────────────
    op.create_table(
        "slack_integration",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("slack_team_id", sa.String(length=64), nullable=False),
        sa.Column("slack_team_name", sa.String(length=255), nullable=True),
        sa.Column("bot_user_id", sa.String(length=64), nullable=True),
        sa.Column("bot_token_encrypted", sa.Text(), nullable=False),
        sa.Column("default_channel_id", sa.String(length=64), nullable=True),
        sa.Column("default_channel_name", sa.String(length=255), nullable=True),
        sa.Column("installed_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Restore the prior notifications.kind CHECK vocabulary.
    _OLD_NOTIFICATION_KINDS = (
        "action_item_assigned",
        "action_item_comment",
        "action_item_returned",
        "action_item_due_soon",
        "action_item_overdue",
        "manager_review_completed",
        "scorecard_review_assigned",
        "system",
        "other",
    )
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        "kind IN (" + ", ".join(f"'{k}'" for k in _OLD_NOTIFICATION_KINDS) + ")",
    )

    op.drop_table("slack_integration")
    op.drop_index("ix_coaching_notes_tenant", table_name="coaching_notes")
    op.drop_index("ix_coaching_notes_assigned", table_name="coaching_notes")
    op.drop_table("coaching_notes")
    op.drop_table("alert_channel_config")
    op.drop_index(
        "ix_manager_recommendations_tenant_open",
        table_name="manager_recommendations",
    )
    op.drop_table("manager_recommendations")
    op.drop_index(
        "ux_manager_alerts_active_fingerprint",
        table_name="manager_alerts",
    )
    op.drop_index("ix_manager_alerts_tenant_open", table_name="manager_alerts")
    op.drop_table("manager_alerts")
