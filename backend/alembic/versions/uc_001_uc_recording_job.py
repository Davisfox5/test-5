"""Stream 2 — UC vendor recording webhook idempotency table.

Adds the single ``uc_recording_jobs`` table that anchors the lifecycle
of RingCentral / Webex Calling / Zoom Phone recording webhooks. Every
inbound, signature-verified webhook upserts one row keyed on
``(provider, external_call_id)``. The Celery ``fetch_uc_recording``
task then drives the row through the state machine (pending →
in_progress → fetched → dispatched → done, with ``failed`` as the
terminal-error state).

Schema mirrors :class:`UcRecordingJob` in ``backend/app/models.py``.

Constraints + indexes:

* Unique on ``(provider, external_call_id)`` — late-arriving duplicate
  webhooks are no-ops.
* CHECK on ``state`` for the six legal values.
* Index on ``tenant_id`` for the dominant scan pattern.
* Index on ``state`` so the worker can poll for stuck jobs.

Branches off the same parent as the other Stream-2..4 migrations
(``z3b4c5d6e7f8``); the integration verification step will run
``alembic merge`` on the parallel heads.

Revision ID: uc_001_uc_recording_job
Revises: z3b4c5d6e7f8
Create Date: 2026-05-07
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "uc_001_uc_recording_job"
down_revision: Union[str, None] = "z3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGAL_STATES = (
    "pending",
    "in_progress",
    "fetched",
    "dispatched",
    "done",
    "failed",
)


def upgrade() -> None:
    op.create_table(
        "uc_recording_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "integration_id",
            UUID(as_uuid=True),
            sa.ForeignKey("integrations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interaction_id",
            UUID(as_uuid=True),
            sa.ForeignKey("interactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_call_id", sa.String(), nullable=False),
        sa.Column("recording_id", sa.String(), nullable=False),
        sa.Column("recording_url", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("started_at_provider", sa.String(), nullable=True),
        sa.Column("direction", sa.String(), nullable=True),
        sa.Column("caller_phone", sa.String(), nullable=True),
        sa.Column("callee_phone", sa.String(), nullable=True),
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "state",
            sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "provider",
            "external_call_id",
            name="uq_uc_recording_jobs_provider_call",
        ),
        sa.CheckConstraint(
            "state IN ("
            + ", ".join(f"'{s}'" for s in _LEGAL_STATES)
            + ")",
            name="ck_uc_recording_jobs_state",
        ),
    )
    op.create_index(
        "ix_uc_recording_jobs_tenant_id",
        "uc_recording_jobs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_uc_recording_jobs_state",
        "uc_recording_jobs",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index("ix_uc_recording_jobs_state", table_name="uc_recording_jobs")
    op.drop_index("ix_uc_recording_jobs_tenant_id", table_name="uc_recording_jobs")
    op.drop_table("uc_recording_jobs")
