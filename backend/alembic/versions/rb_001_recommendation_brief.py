"""Recommendation briefs: per-recommendation enrichment output.

Adds ``brief`` (JSONB) and ``enriched_at`` to ``manager_recommendations``.
The enrichment pass (``recommendation_enrichment``) composes a
situation-specific brief for each customer-targeted recommendation from
the account's full context (interactions, concerns, commitments, support
cases, renewal state, KB matches). ``brief`` stays NULL when enrichment
is disabled or fails; ``rationale`` remains the fallback display text.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "rb_001_recommendation_brief"
down_revision: Union[str, None] = "as01f5b7c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "manager_recommendations",
        sa.Column("brief", JSONB(), nullable=True),
    )
    op.add_column(
        "manager_recommendations",
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("manager_recommendations", "enriched_at")
    op.drop_column("manager_recommendations", "brief")
