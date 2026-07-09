"""Postgres full-text search: GIN expression index on interactions.

Replaces the Elasticsearch-backed transcript search with Postgres FTS.
Uses a GIN **expression** index (no stored column, trigger, or backfill)
built with ``CREATE INDEX CONCURRENTLY`` so it never rewrites the table or
blocks writes — safe to apply to a live, populated ``interactions`` table
within the deploy's release-command window. DDL comes from
``backend.app.search_ddl`` so the statements shipped are the ones the
search tests prove.

Revision ID: pg_fts_001_interaction_search
Revises: ae_001_auto_execution_policy
"""

from alembic import op

from backend.app.search_ddl import create_index_concurrent, drop_statements

# revision identifiers, used by Alembic.
revision = "pg_fts_001_interaction_search"
down_revision = "ae_001_auto_execution_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction — the
    # autocommit block drops Alembic's surrounding transaction for these
    # statements.
    with op.get_context().autocommit_block():
        for stmt in create_index_concurrent():
            op.execute(stmt)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for stmt in drop_statements():
            op.execute(stmt)
