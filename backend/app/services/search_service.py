"""Postgres full-text search over interactions.

Replaces the former Elasticsearch client. Search runs against the
generated ``search_vector`` tsvector column + GIN index (see
``backend/app/search_ddl.py``) and is scoped to the tenant explicitly, so
it rides the RLS backstop rather than maintaining a per-tenant ES index.

Because ``search_vector`` is a STORED generated column derived from the
interaction row itself, there is no separate indexing step: writing the
row is what updates the index. ``index_interaction`` / ``ensure_index``
are therefore no-ops, kept only so existing call sites don't need to know
the backend changed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class SearchService:
    """Full-text search over interactions, backed by Postgres."""

    # ── Index management (no-ops under Postgres FTS) ─────────

    async def ensure_index(self, tenant_id: str) -> None:
        """No-op. The ``search_vector`` column + GIN index are created by
        the ``pg_fts_001`` migration and maintained by Postgres."""
        return None

    async def index_interaction(
        self,
        interaction_id: str,
        tenant_id: str,
        data: dict,
    ) -> None:
        """No-op. ``search_vector`` is a generated column derived from the
        interaction row, so the row write already updated the index. Kept
        for call-site compatibility with the analysis pipeline."""
        return None

    # ── Search ──────────────────────────────────────────────

    async def search(
        self,
        db: AsyncSession,
        tenant_id: str,
        query: str,
        channel: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """Full-text search with a highlighted transcript excerpt, scoped
        to ``tenant_id``.

        Returns a list of dicts with keys: interaction_id, score,
        highlights, summary, channel, created_at.
        """
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, min(int(limit), 100))

        # websearch_to_tsquery accepts free-form user input (quotes, OR,
        # leading -) and never raises on syntax, unlike to_tsquery.
        # NB: use CAST(:bind AS type), not ":bind::type" — SQLAlchemy's
        # text() bind parser chokes on a bind immediately followed by the
        # "::" cast operator ("syntax error at or near :").
        conditions = [
            "tenant_id = CAST(:tenant_id AS uuid)",
            "search_vector @@ websearch_to_tsquery('english', :q)",
        ]
        params: Dict[str, Any] = {"tenant_id": tenant_id, "q": q, "limit": limit}

        if channel:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if agent_id:
            conditions.append("agent_id = CAST(:agent_id AS uuid)")
            params["agent_id"] = agent_id
        if date_from:
            conditions.append("created_at >= CAST(:date_from AS timestamptz)")
            params["date_from"] = date_from
        if date_to:
            conditions.append("created_at <= CAST(:date_to AS timestamptz)")
            params["date_to"] = date_to

        where = " AND ".join(conditions)
        sql = text(
            f"""
            SELECT
                id,
                insights,
                channel,
                created_at,
                ts_rank(search_vector, websearch_to_tsquery('english', :q)) AS rank,
                ts_headline(
                    'english',
                    coalesce(raw_text, ''),
                    websearch_to_tsquery('english', :q),
                    'StartSel="<em>", StopSel="</em>", MaxFragments=3, '
                    'MinWords=5, MaxWords=30'
                ) AS highlight
            FROM interactions
            WHERE {where}
            ORDER BY rank DESC, created_at DESC
            LIMIT :limit
            """
        )

        rows = (await db.execute(sql, params)).mappings().all()

        results: List[dict] = []
        for r in rows:
            insights = r["insights"] if isinstance(r["insights"], dict) else {}
            highlight = r["highlight"]
            created = r["created_at"]
            results.append(
                {
                    "interaction_id": str(r["id"]),
                    "score": float(r["rank"]) if r["rank"] is not None else None,
                    "highlights": [highlight] if highlight else [],
                    "summary": insights.get("summary", "") or "",
                    "channel": r["channel"] or "",
                    "created_at": created.isoformat() if created is not None else None,
                }
            )
        return results

    async def close(self) -> None:
        """No-op. No external client to close."""
        return None
