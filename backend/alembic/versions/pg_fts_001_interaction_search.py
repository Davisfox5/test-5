"""Postgres full-text search: generated search_vector column + GIN index.

Replaces the Elasticsearch-backed transcript search with Postgres FTS —
no external cluster, and search rides the existing RLS tenant scoping
instead of a per-tenant ES index. The DDL comes from
``backend.app.search_ddl`` so the statements shipped here are the ones the
search tests prove (same discipline as the RLS migrations).

The owner connection Alembic runs on already holds the ``interactions``
table; adding a generated column + GIN index is a metadata + build
operation and does not interact with the row-level security policies.

Revision ID: pg_fts_001_interaction_search
Revises: ae_001_auto_execution_policy
"""

from alembic import op

from backend.app.search_ddl import create_statements, drop_statements

# revision identifiers, used by Alembic.
revision = "pg_fts_001_interaction_search"
down_revision = "ae_001_auto_execution_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for stmt in create_statements():
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_statements():
        op.execute(stmt)
