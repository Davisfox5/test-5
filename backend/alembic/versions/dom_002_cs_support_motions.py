"""CS + Support motions: scope manager alerts/recs by domain, add SupportCase.

Builds on ``dom_001_domain_scopes``. Two purposes:

1. **Domain-scope the manager surfaces.** ``manager_alerts`` and
   ``manager_recommendations`` get a ``domain`` column so the multi-tab
   Manager portal can filter the narrative / alerts / recommendations
   queue per motion. Backfilled to each tenant's ``default_domain``.
2. **First-class IT-Support case object.** Unlike Sales (transactional
   interactions) and CS (account-level relationships), Support has a
   ticket lifecycle that spans multiple interactions. ``support_cases``
   is the join object: a case has many ``interactions`` via the new
   nullable ``interactions.support_case_id`` FK.

Both pieces are additive — no existing row's behaviour changes. The
backfill is deterministic and idempotent.

Revision ID: dom_002_cs_support_motions
Revises: dom_001_domain_scopes
Create Date: 2026-05-31

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql


revision: str = "dom_002_cs_support_motions"
down_revision: Union[str, None] = "dom_001_domain_scopes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CANONICAL_DOMAINS = ("sales", "customer_service", "it_support", "generic")


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    jsonb = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()

    # ── manager_alerts.domain ───────────────────────────────────────────
    op.add_column(
        "manager_alerts",
        sa.Column("domain", sa.String(length=32), nullable=True),
    )
    op.execute(
        text(
            """
            UPDATE manager_alerts ma
            SET domain = COALESCE(t.default_domain, 'generic')
            FROM tenants t
            WHERE ma.tenant_id = t.id
              AND ma.domain IS NULL
            """
        )
    )
    op.create_check_constraint(
        "ck_manager_alerts_domain",
        "manager_alerts",
        "domain IS NULL OR domain IN ('sales', 'customer_service', 'it_support', 'generic')",
    )
    op.create_index(
        "ix_manager_alerts_tenant_domain_opened",
        "manager_alerts",
        ["tenant_id", "domain", "opened_at"],
    )

    # ── manager_recommendations.domain ──────────────────────────────────
    op.add_column(
        "manager_recommendations",
        sa.Column("domain", sa.String(length=32), nullable=True),
    )
    op.execute(
        text(
            """
            UPDATE manager_recommendations mr
            SET domain = COALESCE(t.default_domain, 'generic')
            FROM tenants t
            WHERE mr.tenant_id = t.id
              AND mr.domain IS NULL
            """
        )
    )
    op.create_check_constraint(
        "ck_manager_recommendations_domain",
        "manager_recommendations",
        "domain IS NULL OR domain IN ('sales', 'customer_service', 'it_support', 'generic')",
    )
    op.create_index(
        "ix_manager_recs_tenant_domain_status",
        "manager_recommendations",
        ["tenant_id", "domain", "status"],
    )

    # ── support_cases ───────────────────────────────────────────────────
    #
    # The IT-Support motion's anchor object. A case groups every
    # interaction (call, email, chat) belonging to one customer's
    # issue from the moment it opens until it resolves and closes.
    # Sales and CS don't need this — sales calls are transactional
    # and CS works at the account level — so the relationship lives
    # only on Interaction as a nullable FK.
    op.create_table(
        "support_cases",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.dialects.postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assigned_to",
            sa.dialects.postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        # Lifecycle: open -> in_progress -> escalated|resolved -> closed.
        # ``escalated`` is a working state (still actively worked, raised
        # to a higher tier); ``resolved`` means the customer's problem is
        # fixed and we're waiting on a final confirmation/close window.
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="open",
        ),
        # high | medium | low — defaults to medium to avoid forcing the
        # creating agent to choose at open time.
        sa.Column(
            "priority",
            sa.String(length=16),
            nullable=False,
            server_default="medium",
        ),
        # First-contact-resolution flag: True iff the case was resolved
        # within the first interaction on it. Stamped at resolve time.
        sa.Column("first_contact_resolution", sa.Boolean(), nullable=True),
        # CSAT score 1-5 collected post-resolution (NULL when not
        # gathered yet). The detector uses this for ``csat_drop_support``.
        sa.Column("csat_score", sa.Integer(), nullable=True),
        # Optional rich extras (KB articles consulted, prior cases this
        # one was linked to as a duplicate, escalation chain, etc.).
        sa.Column("metadata", jsonb, nullable=False, server_default="{}"),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'escalated', 'resolved', 'closed')",
            name="ck_support_cases_status",
        ),
        sa.CheckConstraint(
            "priority IN ('high', 'medium', 'low')",
            name="ck_support_cases_priority",
        ),
        sa.CheckConstraint(
            "csat_score IS NULL OR csat_score BETWEEN 1 AND 5",
            name="ck_support_cases_csat",
        ),
    )
    op.create_index(
        "ix_support_cases_tenant_status",
        "support_cases",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_support_cases_tenant_opened",
        "support_cases",
        ["tenant_id", "opened_at"],
    )
    op.create_index(
        "ix_support_cases_assigned_to",
        "support_cases",
        ["assigned_to"],
    )

    # ── interactions.support_case_id ────────────────────────────────────
    op.add_column(
        "interactions",
        sa.Column(
            "support_case_id",
            sa.dialects.postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36),
            sa.ForeignKey("support_cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_interactions_support_case",
        "interactions",
        ["support_case_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_interactions_support_case", table_name="interactions")
    op.drop_column("interactions", "support_case_id")
    op.drop_index("ix_support_cases_assigned_to", table_name="support_cases")
    op.drop_index("ix_support_cases_tenant_opened", table_name="support_cases")
    op.drop_index("ix_support_cases_tenant_status", table_name="support_cases")
    op.drop_table("support_cases")
    op.drop_index("ix_manager_recs_tenant_domain_status", table_name="manager_recommendations")
    op.drop_constraint("ck_manager_recommendations_domain", "manager_recommendations", type_="check")
    op.drop_column("manager_recommendations", "domain")
    op.drop_index("ix_manager_alerts_tenant_domain_opened", table_name="manager_alerts")
    op.drop_constraint("ck_manager_alerts_domain", "manager_alerts", type_="check")
    op.drop_column("manager_alerts", "domain")
