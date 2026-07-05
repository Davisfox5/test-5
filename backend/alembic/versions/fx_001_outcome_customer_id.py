"""outcome events — first-class customer_id

Revision ID: fx_001_outcome_customer_id
Revises: eb_001_email_backfill_jobs
Create Date: 2026-07-02 12:00:00.000000

Adds ``outcome_event_ingestions.customer_id`` — optional first-class
customer attribution on posted outcome events (validated against the
interaction's resolved customer at ingest), so calibration/reporting can
aggregate per customer without joining through interactions. Consumers
(Flex) previously smuggled the id into ``event_id`` / ``metadata``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "fx_001_outcome_customer_id"
down_revision: Union[str, None] = "eb_001_email_backfill_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outcome_event_ingestions",
        sa.Column("customer_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_outcome_event_ingestions_customer_id",
        "outcome_event_ingestions",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_outcome_event_ingestions_customer_id",
        "outcome_event_ingestions",
        ["customer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outcome_event_ingestions_customer_id",
        table_name="outcome_event_ingestions",
    )
    op.drop_constraint(
        "fk_outcome_event_ingestions_customer_id",
        "outcome_event_ingestions",
        type_="foreignkey",
    )
    op.drop_column("outcome_event_ingestions", "customer_id")
