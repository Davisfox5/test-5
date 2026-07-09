"""Postgres full-text search DDL for the ``interactions`` table.

Transcript search is backed by a GIN **expression** index over a weighted
tsvector built from the interaction row's own columns — no stored column,
no trigger, no backfill. That makes the migration a single
``CREATE INDEX CONCURRENTLY`` that never rewrites the table or holds a
long ``ACCESS EXCLUSIVE`` lock, so it's safe to apply to a live, populated
``interactions`` table inside the deploy's release-command window.

(The first cut used a STORED generated column; adding one rewrites the
entire table under an exclusive lock and blew past Fly's 5-minute release
timeout. An expression index avoids the rewrite entirely.)

The SAME expression string (``SEARCH_VECTOR_EXPR``) is used by the index
and by ``SearchService``'s WHERE / rank clauses, so the planner can
satisfy the ``@@`` match straight from the index. It's centralized here so
the migration, the service, and the tests all prove the identical
expression — the discipline ``backend/app/rls.py`` uses for RLS policies.
"""

from __future__ import annotations

from typing import List

# GIN index name — referenced by the migration, the service's expected
# plan, the tests, and the deep readiness probe.
SEARCH_INDEX_NAME = "ix_interactions_fts"

# Weighted tsvector over the interaction's own columns.
#
# IMPORTANT: every part is IMMUTABLE (the two-argument
# ``to_tsvector('english', ...)`` form, ``setweight``, ``||``, ``coalesce``,
# ``->>``), which an expression index requires. The one-argument
# ``to_tsvector`` is only STABLE and would be rejected — keep the explicit
# ``'english'`` config.
#
# Weights mirror the old Elasticsearch field boosts:
#   A = transcript body (raw_text)      — was transcript_text^3
#   B = analysis summary                — was summary^2
#   C = title/subject + topics          — was topics / metadata
SEARCH_VECTOR_EXPR = (
    "setweight(to_tsvector('english', coalesce(raw_text, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(insights->>'summary', '')), 'B') || "
    "setweight(to_tsvector('english', "
    "coalesce(title, '') || ' ' || coalesce(subject, '')), 'C') || "
    "setweight(to_tsvector('english', coalesce(insights->>'topics', '')), 'C')"
)


def create_index_concurrent() -> List[str]:
    """Non-blocking index build for migrations / live databases.

    Must run OUTSIDE a transaction (``CREATE INDEX CONCURRENTLY`` forbids
    it). The leading ``DROP INDEX IF EXISTS`` heals a prior failed
    CONCURRENTLY build, which would otherwise leave an INVALID index that
    ``IF NOT EXISTS`` treats as already-present.
    """
    return [
        f"DROP INDEX IF EXISTS {SEARCH_INDEX_NAME}",
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {SEARCH_INDEX_NAME} "
        f"ON interactions USING gin (({SEARCH_VECTOR_EXPR}))",
    ]


def create_index_plain() -> List[str]:
    """Blocking build — fine for tests / fresh schemas where nothing else
    is touching the table and the transactional simplicity is worth it."""
    return [
        f"CREATE INDEX IF NOT EXISTS {SEARCH_INDEX_NAME} "
        f"ON interactions USING gin (({SEARCH_VECTOR_EXPR}))",
    ]


def drop_statements() -> List[str]:
    return [f"DROP INDEX IF EXISTS {SEARCH_INDEX_NAME}"]
