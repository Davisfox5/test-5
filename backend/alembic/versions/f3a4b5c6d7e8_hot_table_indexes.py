"""Tenant-id indexes on hot tables + composite (tenant_id, created_at) where
query patterns sort recent-first.

Every tenant-scoped list query today does ``WHERE tenant_id = :tid [ORDER BY
created_at DESC] LIMIT N``. Without these indexes Postgres sequential-scans
the whole table and sorts in memory. With them the planner does an index
range scan straight into the desired page.

We build each index with ``CREATE INDEX CONCURRENTLY`` so a running prod
deployment doesn't lock writes for the duration of the build. That forces
each statement outside the migration's implicit transaction — we do that
via ``with_autocommit_block`` on Alembic's context.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-04-21 08:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers
revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
# Alembic honors this flag when generating per-statement autocommit scope.
disable_per_migration_transaction = True


# (table, column_expr, optional_suffix)
_TENANT_INDEXES: Sequence[tuple[str, str, str]] = (
    # (tenant_id, created_at DESC) — sort-paginated list endpoints.
    ("interactions", "tenant_id, created_at DESC", "tenant_created"),
    ("contacts", "tenant_id, created_at DESC", "tenant_created"),
    ("interaction_snippets", "tenant_id, created_at DESC", "tenant_created"),
    ("interaction_comments", "tenant_id, created_at DESC", "tenant_created"),
    ("action_items", "tenant_id, created_at DESC", "tenant_created"),
    ("linda_chat_messages", "tenant_id, created_at DESC", "tenant_created"),
    ("feedback_events", "tenant_id, created_at DESC", "tenant_created"),
    ("transcript_corrections", "tenant_id, created_at DESC", "tenant_created"),
    ("insight_quality_scores", "tenant_id, created_at DESC", "tenant_created"),
    ("interaction_scores", "tenant_id, created_at DESC", "tenant_created"),
    ("interaction_features", "tenant_id, created_at DESC", "tenant_created"),
    ("delta_reports", "tenant_id, created_at DESC", "tenant_created"),
    ("tenant_insights", "tenant_id, created_at DESC", "tenant_created"),
    ("correction_events", "tenant_id, created_at DESC", "tenant_created"),
    ("write_proposals", "tenant_id, created_at DESC", "tenant_created"),
    ("kb_documents", "tenant_id, created_at DESC", "tenant_created"),
    ("demo_email_captures", "tenant_id, created_at DESC", "tenant_created"),
    # (tenant_id, last_message_at DESC NULLS LAST) — conversations list.
    (
        "conversations",
        "tenant_id, last_message_at DESC NULLS LAST",
        "tenant_last_message",
    ),
    # Plain tenant_id — callers only filter, don't sort.
    ("users", "tenant_id", "tenant"),
    ("api_keys", "tenant_id", "tenant"),
    ("webhooks", "tenant_id", "tenant"),
    ("customers", "tenant_id", "tenant"),
    ("scorecard_templates", "tenant_id", "tenant"),
    ("live_sessions", "tenant_id", "tenant"),
    ("integrations", "tenant_id", "tenant"),
    ("interaction_attachments", "tenant_id", "tenant"),
    ("client_profiles", "tenant_id", "tenant"),
    ("agent_profiles", "tenant_id", "tenant"),
    ("manager_profiles", "tenant_id", "tenant"),
    ("business_profiles", "tenant_id", "tenant"),
    ("scorer_versions", "tenant_id", "tenant"),
    ("email_sync_cursors", "tenant_id", "tenant"),
    ("campaigns", "tenant_id", "tenant"),
    ("campaign_recipients", "tenant_id", "tenant"),
    ("campaign_events", "tenant_id", "tenant"),
    ("tenant_prompt_configs", "tenant_id", "tenant"),
    ("vocabulary_candidates", "tenant_id", "tenant"),
    ("evaluation_reference_sets", "tenant_id", "tenant"),
    ("wer_metrics", "tenant_id", "tenant"),
)


def upgrade() -> None:
    # Each CREATE INDEX CONCURRENTLY must run outside a transaction.
    with op.get_context().autocommit_block():
        for table, expr, suffix in _TENANT_INDEXES:
            idx_name = f"ix_{table}_{suffix}"
            op.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{idx_name}" '
                f'ON "{table}" ({expr})'
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table, _expr, suffix in _TENANT_INDEXES:
            idx_name = f"ix_{table}_{suffix}"
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{idx_name}"')
