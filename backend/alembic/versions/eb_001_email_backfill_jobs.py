"""Email backfill jobs — historical mailbox import ("sync last N days").

Adds the single ``email_backfill_jobs`` table backing the new
``POST /api/v1/email/backfill`` endpoint. One row per import run; the
Celery task ``email_backfill_run`` drives it through
queued → running → done/error while incrementing the fetched /
ingested / skipped counters, and the row itself is the job handle the
status endpoint returns.

Schema mirrors :class:`EmailBackfillJob` in ``backend/app/models.py``.

Constraints + indexes:

* CHECK on ``status`` for the four legal values.
* Index on ``tenant_id`` — the status endpoint and the
  one-active-job-per-provider guard both scan by tenant.
* Partial UNIQUE index on ``(tenant_id, provider)`` WHERE status IN
  ('queued','running') — makes the one-live-job-per-provider guarantee
  a schema fact instead of a racy SELECT-then-INSERT in the endpoint.
* ``heartbeat_at`` — stamped by the worker at every checkpoint commit
  so stale ``running`` rows (crashed worker) can be detected, taken
  over, or superseded.
* Composite index on ``interactions (tenant_id, provider_message_id)``
  — the per-message dedupe probe both ``ingest_email`` and the backfill
  run; previously it filtered the tenant's entire interaction history.

Revision ID: eb_001_email_backfill_jobs
Revises: rb_001_recommendation_brief
Create Date: 2026-07-02
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "eb_001_email_backfill_jobs"
down_revision: Union[str, None] = "rb_001_recommendation_brief"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_backfill_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "integration_id",
            UUID(as_uuid=True),
            sa.ForeignKey("integrations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ingested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'error')",
            name="ck_email_backfill_jobs_status",
        ),
    )
    op.create_index(
        "ix_email_backfill_jobs_tenant_id", "email_backfill_jobs", ["tenant_id"]
    )
    op.create_index(
        "uq_email_backfill_jobs_active",
        "email_backfill_jobs",
        ["tenant_id", "provider"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_interactions_tenant_provider_message_id",
        "interactions",
        ["tenant_id", "provider_message_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interactions_tenant_provider_message_id", table_name="interactions"
    )
    op.drop_index("uq_email_backfill_jobs_active", table_name="email_backfill_jobs")
    op.drop_index("ix_email_backfill_jobs_tenant_id", table_name="email_backfill_jobs")
    op.drop_table("email_backfill_jobs")
