"""Postgres full-text search DDL for the ``interactions`` table.

Single source of truth for the generated ``search_vector`` column + GIN
index that backs transcript search. Both the Alembic migration
(``pg_fts_001_interaction_search``) and the search tests import these
statements, so the DDL shipped is the DDL the tests prove — the same
pattern ``backend/app/rls.py`` uses for the RLS policies.

This replaces the former Elasticsearch cluster: search now rides Postgres
(no external service) and the existing RLS tenant scoping, rather than a
per-tenant ES index.
"""

from __future__ import annotations

from typing import List

# GIN index name — referenced by the migration, the tests, and the deep
# readiness probe.
SEARCH_INDEX_NAME = "ix_interactions_search_vector"

# The weighted tsvector expression.
#
# IMPORTANT: the two-argument ``to_tsvector('english', ...)`` form is
# IMMUTABLE, which a STORED generated column requires. The one-argument
# form depends on the ``default_text_search_config`` GUC and is only
# STABLE — Postgres rejects it in a generated column. Do not drop the
# explicit ``'english'`` config.
#
# Weights mirror the old Elasticsearch field boosts:
#   A = transcript body (raw_text)      — was transcript_text^3
#   B = analysis summary                — was summary^2
#   C = title/subject + topics          — was topics / metadata
_SEARCH_VECTOR_EXPR = (
    "setweight(to_tsvector('english', coalesce(raw_text, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(insights->>'summary', '')), 'B') || "
    "setweight(to_tsvector('english', "
    "coalesce(title, '') || ' ' || coalesce(subject, '')), 'C') || "
    "setweight(to_tsvector('english', coalesce(insights->>'topics', '')), 'C')"
)


def create_statements() -> List[str]:
    """DDL to add the generated ``search_vector`` column + its GIN index.

    Idempotent (``IF NOT EXISTS``) so the migration and the test fixtures
    can both run it without ordering assumptions.
    """
    return [
        "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS search_vector tsvector "
        f"GENERATED ALWAYS AS ({_SEARCH_VECTOR_EXPR}) STORED",
        f"CREATE INDEX IF NOT EXISTS {SEARCH_INDEX_NAME} "
        "ON interactions USING gin (search_vector)",
    ]


def drop_statements() -> List[str]:
    """Inverse of :func:`create_statements` (index first, then column)."""
    return [
        f"DROP INDEX IF EXISTS {SEARCH_INDEX_NAME}",
        "ALTER TABLE interactions DROP COLUMN IF EXISTS search_vector",
    ]
