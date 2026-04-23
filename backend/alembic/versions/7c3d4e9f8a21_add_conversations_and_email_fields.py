"""add conversations, email-sync cursor, and email fields on interactions

Revision ID: 7c3d4e9f8a21
Revises: 550a40162883
Create Date: 2026-04-18 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "7c3d4e9f8a21"
down_revision: Union[str, None] = "550a40162883"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── conversations ────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("thread_key", sa.String(), nullable=True),
        sa.Column("classification", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("insights", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_thread_key", "conversations", ["thread_key"], unique=False
    )
    op.create_index(
        "ix_conversations_tenant_thread",
        "conversations",
        ["tenant_id", "thread_key"],
        unique=False,
    )

    # ── email_sync_cursors ───────────────────────────────────────────
    op.create_table(
        "email_sync_cursors",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("history_id", sa.String(), nullable=True),
        sa.Column("delta_link", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["integration_id"], ["integrations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("integration_id"),
    )

    # ── interactions: email columns + conversation_id FK ─────────────
    op.add_column("interactions", sa.Column("conversation_id", sa.UUID(), nullable=True))
    op.add_column("interactions", sa.Column("from_address", sa.String(), nullable=True))
    op.add_column("interactions", sa.Column("to_addresses", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"))
    op.add_column("interactions", sa.Column("cc_addresses", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"))
    op.add_column("interactions", sa.Column("subject", sa.String(), nullable=True))
    op.add_column("interactions", sa.Column("message_id", sa.String(), nullable=True))
    op.add_column("interactions", sa.Column("in_reply_to", sa.String(), nullable=True))
    op.add_column("interactions", sa.Column("references", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"))
    op.add_column("interactions", sa.Column("provider_message_id", sa.String(), nullable=True))
    op.add_column("interactions", sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("interactions", sa.Column("classification", sa.String(), nullable=True))
    op.add_column("interactions", sa.Column("classification_confidence", sa.Float(), nullable=True))

    op.create_foreign_key(
        "fk_interactions_conversation",
        source_table="interactions",
        referent_table="conversations",
        local_cols=["conversation_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_interactions_message_id", "interactions", ["message_id"]
    )
    op.create_index(
        "ix_interactions_conversation_id", "interactions", ["conversation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_interactions_conversation_id", table_name="interactions")
    op.drop_constraint("uq_interactions_message_id", "interactions", type_="unique")
    op.drop_constraint("fk_interactions_conversation", "interactions", type_="foreignkey")
    for col in (
        "classification_confidence", "classification", "is_internal",
        "provider_message_id", "references", "in_reply_to", "message_id",
        "subject", "cc_addresses", "to_addresses", "from_address",
        "conversation_id",
    ):
        op.drop_column("interactions", col)
    op.drop_table("email_sync_cursors")
    op.drop_index("ix_conversations_tenant_thread", table_name="conversations")
    op.drop_index("ix_conversations_thread_key", table_name="conversations")
    op.drop_table("conversations")
