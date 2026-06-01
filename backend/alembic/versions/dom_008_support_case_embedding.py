"""Add embedding column to support_cases for cross-customer trend detection.

Stores the case-subject embedding (Voyage 1024-dim by default) as a
JSONB list of floats. JSONB rather than pgvector because the cluster
job runs deterministically in Python over a few thousand cases per
tenant — perfectly cheap without a vector index, and easier to reason
about during the clustering pass.

Revision ID: dom_008_support_case_embedding
Revises: dom_007_customer_tagged_kb
Create Date: 2026-06-01

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "dom_008_support_case_embedding"
down_revision: Union[str, None] = "dom_007_customer_tagged_kb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    jsonb = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()

    op.add_column(
        "support_cases",
        sa.Column("subject_embedding", jsonb, nullable=True),
    )
    op.add_column(
        "support_cases",
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("support_cases", "embedded_at")
    op.drop_column("support_cases", "subject_embedding")
