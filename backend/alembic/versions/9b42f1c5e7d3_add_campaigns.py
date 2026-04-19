"""add campaigns, recipients, events; attribution fk on interactions

Revision ID: 9b42f1c5e7d3
Revises: 7c3d4e9f8a21
Create Date: 2026-04-18 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "9b42f1c5e7d3"
down_revision: Union[str, None] = "7c3d4e9f8a21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("variant", sa.String(), nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("insights", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_campaigns_tenant_external", "campaigns", ["tenant_id", "external_id"], unique=False
    )

    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("email_address", sa.String(), nullable=True),
        sa.Column("external_message_id", sa.String(), nullable=True),
        sa.Column("rfc822_message_id", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_campaign_recipients_rfc822",
        "campaign_recipients",
        ["rfc822_message_id"],
        unique=False,
    )

    op.create_table(
        "campaign_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("recipient_id", sa.UUID(), nullable=True),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["recipient_id"], ["campaign_recipients.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # Attribution link on interactions
    op.add_column(
        "interactions", sa.Column("campaign_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        "fk_interactions_campaign",
        source_table="interactions",
        referent_table="campaigns",
        local_cols=["campaign_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_interactions_campaign_id", "interactions", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_interactions_campaign_id", table_name="interactions")
    op.drop_constraint("fk_interactions_campaign", "interactions", type_="foreignkey")
    op.drop_column("interactions", "campaign_id")
    op.drop_table("campaign_events")
    op.drop_index("ix_campaign_recipients_rfc822", table_name="campaign_recipients")
    op.drop_table("campaign_recipients")
    op.drop_index("ix_campaigns_tenant_external", table_name="campaigns")
    op.drop_table("campaigns")
