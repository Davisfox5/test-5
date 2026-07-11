"""Cold outreach: prospect pipeline columns, outreach campaigns, members.

- customers: pipeline_status / pipeline_status_changed_at / do_not_contact
  (prospects ARE customers; NULL pipeline_status = not outreach-managed)
- campaigns: kind ('external' | 'outreach'), status, config — the existing
  passive campaign-monitoring table grows the LINDA-originated kind
- campaign_recipients: customer_id + step (one row per delivered touch)
- email_sends: campaign_id + customer_id (audit + daily-throttle counters)
- outreach_members: NEW tenant-scoped table — per-(campaign, prospect)
  sequence state machine. Ships its own RLS policies (rls_002 predates it).

Revision ID: out_001_cold_outreach
Revises: pg_fts_001_interaction_search
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

logger = logging.getLogger("alembic.runtime.migration")

revision = "out_001_cold_outreach"
down_revision = "pg_fts_001_interaction_search"
branch_labels = None
depends_on = None

NEW_TABLES = ["outreach_members"]


def upgrade() -> None:
    # ── customers: pipeline columns ─────────────────────────────────────
    op.add_column("customers", sa.Column("pipeline_status", sa.String(16), nullable=True))
    op.add_column(
        "customers",
        sa.Column("pipeline_status_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column(
            "do_not_contact",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_customers_tenant_pipeline_status",
        "customers",
        ["tenant_id", "pipeline_status"],
    )
    op.create_check_constraint(
        "ck_customers_pipeline_status",
        "customers",
        "pipeline_status IS NULL OR pipeline_status IN "
        "('new', 'queued', 'contacted', 'replied', 'demo', 'won', 'lost', "
        "'do_not_contact')",
    )

    # ── campaigns: outreach kind ────────────────────────────────────────
    op.add_column(
        "campaigns",
        sa.Column("kind", sa.String(), nullable=False, server_default="external"),
    )
    op.add_column(
        "campaigns",
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_campaigns_kind", "campaigns", "kind IN ('external', 'outreach')"
    )
    op.create_check_constraint(
        "ck_campaigns_status",
        "campaigns",
        "status IN ('draft', 'active', 'paused', 'completed', 'archived')",
    )

    # ── campaign_recipients: per-touch attribution ──────────────────────
    op.add_column(
        "campaign_recipients",
        sa.Column("customer_id", sa.UUID(), nullable=True),
    )
    op.add_column("campaign_recipients", sa.Column("step", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_campaign_recipients_customer",
        "campaign_recipients",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── email_sends: campaign audit + throttle counters ─────────────────
    op.add_column("email_sends", sa.Column("campaign_id", sa.UUID(), nullable=True))
    op.add_column("email_sends", sa.Column("customer_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_email_sends_campaign",
        "email_sends",
        "campaigns",
        ["campaign_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_email_sends_customer",
        "email_sends",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_email_send_campaign_sent", "email_sends", ["campaign_id", "sent_at"]
    )

    # ── outreach_members ────────────────────────────────────────────────
    op.create_table(
        "outreach_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="draft_pending"),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("touches_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halt_reason", sa.String(), nullable=True),
        sa.Column("draft_subject", sa.Text(), nullable=True),
        sa.Column("draft_body", sa.Text(), nullable=True),
        sa.Column("draft_status", sa.String(), nullable=True),
        sa.Column(
            "personalization",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "thread_message_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id", "customer_id", name="uq_outreach_member_campaign_customer"
        ),
        sa.CheckConstraint(
            "state IN ('draft_pending', 'needs_approval', 'queued', 'in_sequence', "
            "'replied', 'bounced', 'opted_out', 'completed', 'failed', 'halted')",
            name="ck_outreach_members_state",
        ),
        sa.CheckConstraint(
            "draft_status IS NULL OR draft_status IN "
            "('generating', 'ready', 'approved', 'rejected')",
            name="ck_outreach_members_draft_status",
        ),
    )
    op.create_index("ix_outreach_members_tenant_id", "outreach_members", ["tenant_id"])
    op.create_index("ix_outreach_members_campaign_id", "outreach_members", ["campaign_id"])
    op.create_index("ix_outreach_members_customer_id", "outreach_members", ["customer_id"])
    op.create_index(
        "ix_outreach_members_campaign_state", "outreach_members", ["campaign_id", "state"]
    )
    op.create_index(
        "ix_outreach_members_tenant_state", "outreach_members", ["tenant_id", "state"]
    )

    # ── RLS for the new table (rls_002 rollout predates it) ─────────────
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        from backend.app import rls

        for stmt in rls.rls_statements(tables=NEW_TABLES):
            conn.execute(sa.text(stmt))
        # Grants for the runtime role, mirroring rls_002's posture.
        import os

        role = os.environ.get("APP_DB_ROLE", "linda_app")
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
        if exists:
            conn.execute(
                sa.text(
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE '
                    'outreach_members TO "{}"'.format(role)
                )
            )
        else:
            logger.warning(
                "outreach_members created with RLS but role %r absent — "
                "grant manually once the app role exists.",
                role,
            )


def downgrade() -> None:
    op.drop_index("ix_outreach_members_tenant_state", table_name="outreach_members")
    op.drop_index("ix_outreach_members_campaign_state", table_name="outreach_members")
    op.drop_index("ix_outreach_members_customer_id", table_name="outreach_members")
    op.drop_index("ix_outreach_members_campaign_id", table_name="outreach_members")
    op.drop_index("ix_outreach_members_tenant_id", table_name="outreach_members")
    op.drop_table("outreach_members")

    op.drop_index("ix_email_send_campaign_sent", table_name="email_sends")
    op.drop_constraint("fk_email_sends_customer", "email_sends", type_="foreignkey")
    op.drop_constraint("fk_email_sends_campaign", "email_sends", type_="foreignkey")
    op.drop_column("email_sends", "customer_id")
    op.drop_column("email_sends", "campaign_id")

    op.drop_constraint(
        "fk_campaign_recipients_customer", "campaign_recipients", type_="foreignkey"
    )
    op.drop_column("campaign_recipients", "step")
    op.drop_column("campaign_recipients", "customer_id")

    op.drop_constraint("ck_campaigns_status", "campaigns", type_="check")
    op.drop_constraint("ck_campaigns_kind", "campaigns", type_="check")
    op.drop_column("campaigns", "config")
    op.drop_column("campaigns", "status")
    op.drop_column("campaigns", "kind")

    op.drop_constraint("ck_customers_pipeline_status", "customers", type_="check")
    op.drop_index("ix_customers_tenant_pipeline_status", table_name="customers")
    op.drop_column("customers", "do_not_contact")
    op.drop_column("customers", "pipeline_status_changed_at")
    op.drop_column("customers", "pipeline_status")
