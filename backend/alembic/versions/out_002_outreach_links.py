"""Outreach click tracking: outreach_links redirect-token table.

One row per distinct URL per tracked outreach send. The public
GET /t/{token} endpoint resolves the token and 302s to original_url,
recording a ``click`` CampaignEvent on the way through. See
backend/app/services/outreach/links.py.

The table is in ``rls.AUTH_BOOTSTRAP_TABLES``: the token lookup runs
before any tenant is known (the click comes from a prospect's mail
client, unauthenticated), so its SELECT policy allows an unset GUC —
the emitters in backend.app.rls generate that predicate.

Revision ID: out_002_outreach_links
Revises: out_001_cold_outreach
Create Date: 2026-07-16
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "out_002_outreach_links"
down_revision = "out_001_cold_outreach"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

NEW_TABLES = ["outreach_links"]


def upgrade() -> None:
    op.create_table(
        "outreach_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("member_id", sa.UUID(), nullable=True),
        sa.Column("recipient_id", sa.UUID(), nullable=True),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["outreach_members.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["campaign_recipients.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_outreach_links_token"),
    )
    # The public endpoint's one lookup: token → row.
    op.create_index("ix_outreach_links_token", "outreach_links", ["token"])
    op.create_index("ix_outreach_links_tenant_id", "outreach_links", ["tenant_id"])
    op.create_index("ix_outreach_links_campaign_id", "outreach_links", ["campaign_id"])

    # ── RLS for the new table (rls_002 rollout predates it) ─────────────
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        from backend.app import rls

        for stmt in rls.rls_statements(tables=NEW_TABLES):
            conn.execute(sa.text(stmt))
        # Grants for the runtime role, mirroring out_001's posture.
        import os

        role = os.environ.get("APP_DB_ROLE", "linda_app")
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
        if exists:
            conn.execute(
                sa.text(
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE '
                    'outreach_links TO "{}"'.format(role)
                )
            )
        else:
            logger.warning(
                "outreach_links created with RLS but role %r absent — "
                "grant manually once the app role exists.",
                role,
            )


def downgrade() -> None:
    op.drop_index("ix_outreach_links_campaign_id", table_name="outreach_links")
    op.drop_index("ix_outreach_links_tenant_id", table_name="outreach_links")
    op.drop_index("ix_outreach_links_token", table_name="outreach_links")
    op.drop_table("outreach_links")
