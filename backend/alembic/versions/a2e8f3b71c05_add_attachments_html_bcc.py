"""add email attachments, body_html, bcc_addresses

Revision ID: a2e8f3b71c05
Revises: 9b42f1c5e7d3
Create Date: 2026-04-19 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "a2e8f3b71c05"
down_revision: Union[str, None] = "9b42f1c5e7d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "interactions", sa.Column("body_html", sa.Text(), nullable=True)
    )
    op.add_column(
        "interactions",
        sa.Column(
            "bcc_addresses",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )

    op.create_table(
        "interaction_attachments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("s3_key", sa.String(), nullable=True),
        sa.Column("provider_attachment_id", sa.String(), nullable=True),
        sa.Column("direction", sa.String(), nullable=True),
        sa.Column("inline", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("content_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_interaction_attachments_interaction",
        "interaction_attachments",
        ["interaction_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interaction_attachments_interaction",
        table_name="interaction_attachments",
    )
    op.drop_table("interaction_attachments")
    op.drop_column("interactions", "bcc_addresses")
    op.drop_column("interactions", "body_html")
