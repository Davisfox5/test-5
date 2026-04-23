"""Elasticsearch-backed full-text search for interactions."""

from __future__ import annotations

from typing import Dict, List, Optional

from elasticsearch import AsyncElasticsearch

from backend.app.config import get_settings

settings = get_settings()


class SearchService:
    """Async Elasticsearch client for indexing and searching interactions."""

    def __init__(self) -> None:
        self.es = AsyncElasticsearch(hosts=[settings.ELASTICSEARCH_URL])

    def _index_name(self, tenant_id: str) -> str:
        return f"linda-interactions-{tenant_id}"

    # ── Index management ────────────────────────────────────

    async def ensure_index(self, tenant_id: str) -> None:
        """Create the tenant index with proper mappings if it doesn't exist."""
        index = self._index_name(tenant_id)
        exists = await self.es.indices.exists(index=index)
        if exists:
            return

        mappings = {
            "properties": {
                "interaction_id": {"type": "keyword"},
                "transcript_text": {"type": "text", "analyzer": "standard"},
                "summary": {"type": "text"},
                "topics": {"type": "keyword"},
                "contact_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "company": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "agent_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "agent_id": {"type": "keyword"},
                "channel": {"type": "keyword"},
                "sentiment_score": {"type": "float"},
                "created_at": {"type": "date"},
            }
        }

        await self.es.indices.create(
            index=index,
            body={"mappings": mappings},
        )

    # ── Indexing ────────────────────────────────────────────

    async def index_interaction(
        self,
        interaction_id: str,
        tenant_id: str,
        data: dict,
    ) -> None:
        """Index an interaction document.

        ``data`` should contain keys like transcript_segments (list of dicts
        with a ``text`` field), summary, topics, contact_name, company,
        agent_name, agent_id, channel, sentiment_score, created_at.
        """
        await self.ensure_index(tenant_id)
        index = self._index_name(tenant_id)

        # Join transcript segment texts into a single searchable string
        segments = data.get("transcript_segments", [])
        transcript_text = " ".join(
            seg.get("text", "") for seg in segments if isinstance(seg, dict)
        )

        doc: Dict[str, object] = {
            "interaction_id": interaction_id,
            "transcript_text": transcript_text or data.get("transcript_text", ""),
            "summary": data.get("summary", ""),
            "topics": data.get("topics", []),
            "contact_name": data.get("contact_name", ""),
            "company": data.get("company", ""),
            "agent_name": data.get("agent_name", ""),
            "agent_id": data.get("agent_id", ""),
            "channel": data.get("channel", ""),
            "sentiment_score": data.get("sentiment_score"),
            "created_at": data.get("created_at"),
        }

        await self.es.index(index=index, id=interaction_id, body=doc)

    # ── Search ──────────────────────────────────────────────

    async def search(
        self,
        tenant_id: str,
        query: str,
        channel: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """Full-text search with highlight on transcript_text, filtered by tenant.

        Returns list of dicts with keys: interaction_id, score, highlights,
        summary, channel, created_at.
        """
        index = self._index_name(tenant_id)

        # Check if index exists; return empty results if not
        exists = await self.es.indices.exists(index=index)
        if not exists:
            return []

        # Build bool query
        must = [
            {
                "multi_match": {
                    "query": query,
                    "fields": ["transcript_text^3", "summary^2", "topics", "contact_name", "company"],
                }
            }
        ]

        filters: List[dict] = []
        if channel:
            filters.append({"term": {"channel": channel}})
        if agent_id:
            filters.append({"term": {"agent_id": agent_id}})
        if date_from or date_to:
            range_filter: Dict[str, str] = {}
            if date_from:
                range_filter["gte"] = date_from
            if date_to:
                range_filter["lte"] = date_to
            filters.append({"range": {"created_at": range_filter}})

        body: dict = {
            "query": {
                "bool": {
                    "must": must,
                    "filter": filters,
                }
            },
            "highlight": {
                "fields": {
                    "transcript_text": {
                        "fragment_size": 200,
                        "number_of_fragments": 3,
                    }
                },
                "pre_tags": ["<em>"],
                "post_tags": ["</em>"],
            },
            "size": limit,
            "_source": ["interaction_id", "summary", "channel", "created_at"],
        }

        resp = await self.es.search(index=index, body=body)
        hits = resp.get("hits", {}).get("hits", [])

        results: List[dict] = []
        for hit in hits:
            source = hit.get("_source", {})
            highlights = hit.get("highlight", {}).get("transcript_text", [])
            results.append(
                {
                    "interaction_id": source.get("interaction_id"),
                    "score": hit.get("_score"),
                    "highlights": highlights,
                    "summary": source.get("summary", ""),
                    "channel": source.get("channel", ""),
                    "created_at": source.get("created_at"),
                }
            )

        return results

    # ── Cleanup ─────────────────────────────────────────────

    async def close(self) -> None:
        await self.es.close()
