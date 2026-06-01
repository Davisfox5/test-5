"""CS renewals + KB-request workflow + per-domain alert/Slack overrides.

Builds on ``dom_002_cs_support_motions``. Adds the data model needed by
PR C:

* ``customers.renewal_date`` / ``health_score`` / ``onboarding_status``
  — first-class CS columns the renewals strip + account-health
  timeline read directly. NULL across the board on existing rows
  (backfill is out-of-band via CRM sync; we won't fabricate values
  the migration can't validate).
* ``kb_article_requests`` — replaces the CoachingNote-stub artifact
  the ``update_kb_article`` / ``escalate_recurring_issue``
  recommendations were creating in PR #113. Now those Apply paths
  produce a real KB-edit-request row with its own lifecycle.
* ``alert_domain_config`` — per-(tenant, domain) override row for the
  alert thresholds. Today every detector reads from the single
  ``alert_channel_config`` row, so CS sentiment-drop uses the same
  knob as Sales sentiment-drop. The new override table lets each
  motion tune independently; the detector falls back to the legacy
  row when no override exists.
* ``slack_integration.domain_channel_map`` — per-domain channel
  override JSONB. Today all alerts land in one Slack channel; the
  override maps ``{"sales": "C001", "customer_service": "C002", ...}``
  for tenants that want motion-specific channels.

Revision ID: dom_003_cs_kb_polish
Revises: dom_002_cs_support_motions
Create Date: 2026-05-31

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "dom_003_cs_kb_polish"
down_revision: Union[str, None] = "dom_002_cs_support_motions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    jsonb = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()
    uuid_t = (
        postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)
    )

    # ── customers.renewal_date / health_score / onboarding_status ──────
    op.add_column(
        "customers",
        sa.Column("renewal_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column("health_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column(
            "onboarding_status",
            sa.String(length=32),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_customers_onboarding_status",
        "customers",
        "onboarding_status IS NULL OR onboarding_status IN "
        "('not_started', 'in_progress', 'stalled', 'completed')",
    )
    op.create_check_constraint(
        "ck_customers_health_score_range",
        "customers",
        "health_score IS NULL OR (health_score >= 0 AND health_score <= 100)",
    )
    op.create_index(
        "ix_customers_tenant_renewal_date",
        "customers",
        ["tenant_id", "renewal_date"],
    )

    # ── kb_article_requests ────────────────────────────────────────────
    op.create_table(
        "kb_article_requests",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_user_id",
            uuid_t,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assigned_to",
            uuid_t,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_recommendation_id",
            uuid_t,
            sa.ForeignKey("manager_recommendations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_kb_chunk_id",
            uuid_t,
            sa.ForeignKey("kb_chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("topic", sa.String(length=300), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("proposed_body", sa.Text(), nullable=True),
        # open | in_progress | published | dismissed
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "priority",
            sa.String(length=16),
            nullable=False,
            server_default="medium",
        ),
        sa.Column("metadata", jsonb, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismiss_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'published', 'dismissed')",
            name="ck_kb_article_requests_status",
        ),
        sa.CheckConstraint(
            "priority IN ('high', 'medium', 'low')",
            name="ck_kb_article_requests_priority",
        ),
    )
    op.create_index(
        "ix_kb_article_requests_tenant_status",
        "kb_article_requests",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_kb_article_requests_assigned_to",
        "kb_article_requests",
        ["assigned_to"],
    )

    # ── alert_domain_config (per-(tenant,domain) overrides) ───────────
    #
    # Composite PK (tenant_id, domain) instead of refactoring
    # ``alert_channel_config``'s tenant-only PK (which would be a
    # data migration on a live table). The detector reads this table
    # first; falls back to the legacy single row when no override is
    # present. Each column NULL means "use the legacy ``alert_channel_config``
    # value for this knob".
    op.create_table(
        "alert_domain_config",
        sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("domain", sa.String(length=32), primary_key=True),
        sa.Column(
            "topic_spike_pct_change_threshold",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column("topic_spike_min_volume", sa.Integer(), nullable=True),
        sa.Column("sentiment_drop_threshold", sa.Float(), nullable=True),
        sa.Column("churn_surge_multiplier", sa.Float(), nullable=True),
        sa.Column(
            "methodology_drop_threshold", sa.Float(), nullable=True
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "domain IN ('sales', 'customer_service', 'it_support', 'generic')",
            name="ck_alert_domain_config_domain",
        ),
    )

    # ── slack_integration.domain_channel_map ──────────────────────────
    op.add_column(
        "slack_integration",
        sa.Column(
            "domain_channel_map",
            jsonb,
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("slack_integration", "domain_channel_map")
    op.drop_table("alert_domain_config")
    op.drop_index(
        "ix_kb_article_requests_assigned_to", table_name="kb_article_requests"
    )
    op.drop_index(
        "ix_kb_article_requests_tenant_status", table_name="kb_article_requests"
    )
    op.drop_table("kb_article_requests")
    op.drop_index("ix_customers_tenant_renewal_date", table_name="customers")
    op.drop_constraint(
        "ck_customers_health_score_range", "customers", type_="check"
    )
    op.drop_constraint(
        "ck_customers_onboarding_status", "customers", type_="check"
    )
    op.drop_column("customers", "onboarding_status")
    op.drop_column("customers", "health_score")
    op.drop_column("customers", "renewal_date")
