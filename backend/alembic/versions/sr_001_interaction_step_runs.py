"""interaction_step_runs — durable per-step pipeline ledger

Revision ID: sr_001_interaction_step_runs
Revises: fx_001_outcome_customer_id
Create Date: 2026-07-05 12:00:00.000000

The exactly-once backbone for the interaction pipeline
(docs/complexity/01-pipeline-exactly-once.md §7): one row per
(interaction, step, input-hash), claimed atomically before a paid /
non-idempotent step runs, so retries and duplicate deliveries resume
instead of re-paying LLM calls. Lease fields let a takeover happen when
the claiming worker died mid-step.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers
revision: str = "sr_001_interaction_step_runs"
down_revision: Union[str, None] = "fx_001_outcome_customer_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "interaction_step_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("interaction_id", sa.UUID(), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False,
            server_default="running",
        ),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("output_digest", sa.String(length=256), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["interaction_id"], ["interactions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "interaction_id", "step_key", "input_hash", name="uq_step_run_key"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_step_run_status",
        ),
    )
    op.create_index(
        "ix_interaction_step_runs_tenant_id", "interaction_step_runs", ["tenant_id"]
    )
    op.create_index(
        "ix_interaction_step_runs_interaction_id",
        "interaction_step_runs",
        ["interaction_id"],
    )
    op.create_index(
        "ix_step_runs_tenant_step_status",
        "interaction_step_runs",
        ["tenant_id", "step_key", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_step_runs_tenant_step_status", table_name="interaction_step_runs")
    op.drop_index(
        "ix_interaction_step_runs_interaction_id", table_name="interaction_step_runs"
    )
    op.drop_index(
        "ix_interaction_step_runs_tenant_id", table_name="interaction_step_runs"
    )
    op.drop_table("interaction_step_runs")
