"""KB retrieval — embed a query and return the top-K relevant chunks.

Tenant-scoped by construction. Tracks latency so the vector-health monitor can
observe degradation over time.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import PinnedKBCard
from backend.app.services.kb.embedder import VoyageEmbedder, VoyageEmbedderError
from backend.app.services.kb.vector_health import record_search_latency
from backend.app.services.kb.vector_store import (
    SearchHit,
    VectorStore,
    get_vector_store,
)

logger = logging.getLogger(__name__)


class RetrievalService:
    """Turns a natural-language query into ranked KB chunks."""

    def __init__(
        self,
        embedder: Optional[VoyageEmbedder] = None,
        store: Optional[VectorStore] = None,
    ) -> None:
        self._embedder = embedder or VoyageEmbedder()
        self._store = store or get_vector_store()

    async def search(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        query: str,
        k: int = 3,
        exclude_chunk_ids: Optional[Sequence[uuid.UUID]] = None,
    ) -> List[SearchHit]:
        """Return top-K hits for ``query``, tenant-scoped."""
        query = (query or "").strip()
        if not query:
            return []

        start = time.monotonic()
        try:
            vecs = await self._embedder.embed([query], input_type="query")
        except VoyageEmbedderError:
            logger.exception("Voyage embed failed for query")
            return []
        if not vecs:
            return []

        hits = await self._store.search(
            db,
            tenant_id=tenant_id,
            query_embedding=vecs[0],
            k=k,
            exclude_chunk_ids=exclude_chunk_ids,
        )
        elapsed_ms = (time.monotonic() - start) * 1000.0
        await record_search_latency(tenant_id, elapsed_ms)
        return hits

    async def pinned_chunk_ids(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        contact_id: Optional[uuid.UUID],
    ) -> List[uuid.UUID]:
        """Chunk ids that are already pinned for this contact.

        We exclude these from retrieval suggestions so we don't re-surface a
        card the agent is already looking at.
        """
        if contact_id is None:
            return []
        stmt = select(PinnedKBCard.chunk_id).where(
            PinnedKBCard.tenant_id == tenant_id,
            PinnedKBCard.contact_id == contact_id,
        )
        rows = await db.execute(stmt)
        return [uuid.UUID(str(r[0])) for r in rows.all()]


def hit_to_payload(hit: SearchHit) -> dict:
    """Shape a SearchHit for a WebSocket kb_answer message or JSON API."""
    data = asdict(hit)
    data["chunk_id"] = str(hit.chunk_id)
    data["doc_id"] = str(hit.doc_id)
    return data
