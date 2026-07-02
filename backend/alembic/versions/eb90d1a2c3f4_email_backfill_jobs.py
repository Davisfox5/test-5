"""Email backfill jobs — on-demand "import last N days" runs.

Backs the ``POST /api/v1/email/backfill`` + ``GET .../backfill/{job_id}``
endpoints. A row tracks one backfill run for a connected mailbox: its
status (queued/running/done/error), the window, and live counters
(fetched/ingested/skipped) the status endpoint polls. The actual import
reuses the existing ingest→analyze pipeline and dedupes on
``(tenant_id, provider_message_id)``, so the table only carries
bookkeeping — no message content.

Revision ID: eb90d1a2c3f4
Revises: as01f5b7c9d0
Create Date: 2026-06-30

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "eb90d1a2c3f4"
down_revision: Union[str, None] = "as01f5b7c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    uuid_t = postgresql.UUID(as_uuid=True) if is_postgres else sa.String(36)

    op.create_table(
        "email_backfill_jobs",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column(
            "tenant_id",
            uuid_t,
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "integration_id",
            uuid_t,
            sa.ForeignKey("integrations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(), nullable=False),
        # queued | running | done | error
        sa.Column(
            "status", sa.String(), nullable=False, server_default="queued"
        ),
        sa.Column(
            "window_days", sa.Integer(), nullable=False, server_default="90"
        ),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ingested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'error')",
            name="ck_email_backfill_jobs_status",
        ),
    )
    op.create_index(
        "ix_email_backfill_jobs_tenant_id",
        "email_backfill_jobs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_email_backfill_jobs_tenant_provider",
        "email_backfill_jobs",
        ["tenant_id", "provider"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_backfill_jobs_tenant_provider",
        table_name="email_backfill_jobs",
    )
    op.drop_index(
        "ix_email_backfill_jobs_tenant_id",
        table_name="email_backfill_jobs",
    )
    op.drop_table("email_backfill_jobs")
