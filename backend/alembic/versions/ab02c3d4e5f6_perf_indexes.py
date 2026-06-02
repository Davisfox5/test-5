"""Performance indexes — GIN on JSONB hot paths + tenant+status composites.

Targets the slow queries surfaced by the perf audit:

* ``KBDocument.tags`` and ``InteractionSnippet.tags`` are filtered with the
  JSONB containment operator (``@>``). Without a GIN index Postgres
  sequential-scans the whole table per filter.
* ``Interaction.insights`` is queried by inner keys (``->>'sentiment_score'``,
  ``->>'churn_risk_signal'``) in the analytics dashboard rollups. GIN with
  ``jsonb_path_ops`` handles those efficiently.
* ``(tenant_id, status)`` on ``Interaction`` and ``ActionItem`` powers the
  listing/dashboard filters that exist on both. Existing single-column
  indexes don't combine well with the tenant scope filter.
* ``(tenant_id, created_at)`` on ``customers`` for "recently created" list
  filters and trend rollups.

All ``CREATE INDEX`` statements use ``IF NOT EXISTS`` and target stable,
already-existing columns; the migration is a no-op on a database that
already has them.

Revision ID: ab02c3d4e5f6
Revises: aa02b3c4d5e6
Create Date: 2026-06-01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "ab02c3d4e5f6"
down_revision: Union[str, None] = "aa02b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_kb_docs_tags "
        "ON kb_documents USING GIN (tags)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_interaction_snippets_tags "
        "ON interaction_snippets USING GIN (tags)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_interaction_insights_gin "
        "ON interactions USING GIN (insights jsonb_path_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_interaction_tenant_status "
        "ON interactions (tenant_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_action_items_tenant_status "
        "ON action_items (tenant_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_action_items_tenant_assigned "
        "ON action_items (tenant_id, assigned_to)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_customer_tenant_created "
        "ON customers (tenant_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_customer_tenant_created")
    op.execute("DROP INDEX IF EXISTS ix_action_items_tenant_assigned")
    op.execute("DROP INDEX IF EXISTS ix_action_items_tenant_status")
    op.execute("DROP INDEX IF EXISTS ix_interaction_tenant_status")
    op.execute("DROP INDEX IF EXISTS ix_interaction_insights_gin")
    op.execute("DROP INDEX IF EXISTS ix_interaction_snippets_tags")
    op.execute("DROP INDEX IF EXISTS ix_kb_docs_tags")
