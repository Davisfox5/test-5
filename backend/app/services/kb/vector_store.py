"""Pluggable vector store abstraction.

We support two backends behind the same interface:

* ``pgvector`` — default. Uses the existing Postgres database.
* ``qdrant``  — scaffolded for when pgvector starts hurting. Switch backends
  via the ``VECTOR_BACKEND`` setting and run ``reindex_tenant`` to rebuild.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class ChunkRecord:
    """A chunk ready to be upserted into the vector store."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    doc_id: uuid.UUID
    chunk_idx: int
    text: str
    embedding: List[float]
    doc_title: Optional[str] = None
    source_url: Optional[str] = None


@dataclass
class SearchHit:
    """One search result."""

    chunk_id: uuid.UUID
    doc_id: uuid.UUID
    chunk_idx: int
    text: str
    score: float  # cosine similarity in [0, 1]
    doc_title: Optional[str] = None
    source_url: Optional[str] = None


@runtime_checkable
class VectorStore(Protocol):
    """Common interface for pgvector and Qdrant backends."""

    async def upsert(
        self,
        db: AsyncSession,
        chunks: Sequence[ChunkRecord],
    ) -> None: ...

    async def delete_doc(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        doc_id: uuid.UUID,
    ) -> None: ...

    async def search(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        query_embedding: Sequence[float],
        k: int = 5,
        exclude_chunk_ids: Optional[Sequence[uuid.UUID]] = None,
    ) -> List[SearchHit]: ...


# ──────────────────────────────────────────────────────────
# pgvector implementation
# ──────────────────────────────────────────────────────────


def _vec_literal(v: Sequence[float]) -> str:
    """Format a list of floats as a pgvector literal, e.g. '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


class PgVectorStore:
    """pgvector-backed implementation. No extra infra — uses the app DB."""

    async def upsert(
        self,
        db: AsyncSession,
        chunks: Sequence[ChunkRecord],
    ) -> None:
        if not chunks:
            return

        for c in chunks:
            await db.execute(
                text(
                    "INSERT INTO kb_chunks "
                    "(id, tenant_id, doc_id, chunk_idx, text, token_count, embedding) "
                    "VALUES (:id, :tenant_id, :doc_id, :chunk_idx, :text, :token_count, "
                    "CAST(:embedding AS vector)) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "text = EXCLUDED.text, "
                    "token_count = EXCLUDED.token_count, "
                    "embedding = EXCLUDED.embedding"
                ),
                {
                    "id": str(c.id),
                    "tenant_id": str(c.tenant_id),
                    "doc_id": str(c.doc_id),
                    "chunk_idx": c.chunk_idx,
                    "text": c.text,
                    "token_count": len(c.text) // 4,
                    "embedding": _vec_literal(c.embedding),
                },
            )

    async def delete_doc(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        doc_id: uuid.UUID,
    ) -> None:
        await db.execute(
            text(
                "DELETE FROM kb_chunks "
                "WHERE tenant_id = :tenant_id AND doc_id = :doc_id"
            ),
            {"tenant_id": str(tenant_id), "doc_id": str(doc_id)},
        )

    async def search(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        query_embedding: Sequence[float],
        k: int = 5,
        exclude_chunk_ids: Optional[Sequence[uuid.UUID]] = None,
    ) -> List[SearchHit]:
        params: Dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "q": _vec_literal(query_embedding),
            "k": k,
        }
        exclusion_sql = ""
        if exclude_chunk_ids:
            # Build an IN clause with named params.
            placeholders = []
            for i, cid in enumerate(exclude_chunk_ids):
                key = f"exc_{i}"
                params[key] = str(cid)
                placeholders.append(f":{key}")
            if placeholders:
                exclusion_sql = f" AND c.id NOT IN ({', '.join(placeholders)})"

        sql = (
            "SELECT c.id, c.doc_id, c.chunk_idx, c.text, "
            "1 - (c.embedding <=> CAST(:q AS vector)) AS score, "
            "d.title AS doc_title, d.source_url AS source_url "
            "FROM kb_chunks c "
            "JOIN kb_documents d ON d.id = c.doc_id "
            "WHERE c.tenant_id = :tenant_id"
            f"{exclusion_sql} "
            "ORDER BY c.embedding <=> CAST(:q AS vector) ASC "
            "LIMIT :k"
        )

        result = await db.execute(text(sql), params)
        hits: List[SearchHit] = []
        for row in result.mappings():
            hits.append(
                SearchHit(
                    chunk_id=uuid.UUID(str(row["id"])),
                    doc_id=uuid.UUID(str(row["doc_id"])),
                    chunk_idx=row["chunk_idx"],
                    text=row["text"],
                    score=float(row["score"]),
                    doc_title=row["doc_title"],
                    source_url=row["source_url"],
                )
            )
        return hits


# ──────────────────────────────────────────────────────────
# Qdrant implementation (scaffolded; activated via VECTOR_BACKEND=qdrant)
# ──────────────────────────────────────────────────────────


class QdrantStore:
    """Qdrant-backed implementation.

    The collection is ``kb_chunks`` and every point carries a ``tenant_id``
    payload used as a mandatory filter. Switching from pgvector to Qdrant
    requires running ``reindex_tenant`` for each tenant to populate the
    collection.
    """

    _COLLECTION = "kb_chunks"

    def __init__(self) -> None:
        # Defer the import so pgvector-only deployments don't need the client.
        from qdrant_client import AsyncQdrantClient

        settings = get_settings()
        self._client = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY or None,
        )
        self._dim = settings.VOYAGE_EMBED_DIM

    async def _ensure_collection(self) -> None:
        from qdrant_client.http import models as qm

        existing = await self._client.get_collections()
        names = {c.name for c in existing.collections}
        if self._COLLECTION in names:
            return
        await self._client.create_collection(
            collection_name=self._COLLECTION,
            vectors_config=qm.VectorParams(size=self._dim, distance=qm.Distance.COSINE),
        )
        # Payload index on tenant_id for fast filtering.
        await self._client.create_payload_index(
            collection_name=self._COLLECTION,
            field_name="tenant_id",
            field_schema=qm.PayloadSchemaType.KEYWORD,
        )

    async def upsert(
        self,
        db: AsyncSession,  # noqa: ARG002 — Qdrant doesn't need the SQL session
        chunks: Sequence[ChunkRecord],
    ) -> None:
        if not chunks:
            return

        from qdrant_client.http import models as qm

        await self._ensure_collection()

        points = [
            qm.PointStruct(
                id=str(c.id),
                vector=list(c.embedding),
                payload={
                    "tenant_id": str(c.tenant_id),
                    "doc_id": str(c.doc_id),
                    "chunk_idx": c.chunk_idx,
                    "text": c.text,
                    "doc_title": c.doc_title,
                    "source_url": c.source_url,
                },
            )
            for c in chunks
        ]
        await self._client.upsert(collection_name=self._COLLECTION, points=points)

    async def delete_doc(
        self,
        db: AsyncSession,  # noqa: ARG002
        tenant_id: uuid.UUID,
        doc_id: uuid.UUID,
    ) -> None:
        from qdrant_client.http import models as qm

        await self._client.delete(
            collection_name=self._COLLECTION,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="tenant_id",
                            match=qm.MatchValue(value=str(tenant_id)),
                        ),
                        qm.FieldCondition(
                            key="doc_id",
                            match=qm.MatchValue(value=str(doc_id)),
                        ),
                    ]
                )
            ),
        )

    async def search(
        self,
        db: AsyncSession,  # noqa: ARG002
        tenant_id: uuid.UUID,
        query_embedding: Sequence[float],
        k: int = 5,
        exclude_chunk_ids: Optional[Sequence[uuid.UUID]] = None,
    ) -> List[SearchHit]:
        from qdrant_client.http import models as qm

        must = [
            qm.FieldCondition(
                key="tenant_id",
                match=qm.MatchValue(value=str(tenant_id)),
            )
        ]
        must_not = []
        if exclude_chunk_ids:
            must_not.append(
                qm.HasIdCondition(has_id=[str(cid) for cid in exclude_chunk_ids])
            )

        results = await self._client.search(
            collection_name=self._COLLECTION,
            query_vector=list(query_embedding),
            query_filter=qm.Filter(must=must, must_not=must_not or None),
            limit=k,
            with_payload=True,
        )

        hits: List[SearchHit] = []
        for r in results:
            payload = r.payload or {}
            hits.append(
                SearchHit(
                    chunk_id=uuid.UUID(str(r.id)),
                    doc_id=uuid.UUID(payload["doc_id"]),
                    chunk_idx=int(payload.get("chunk_idx", 0)),
                    text=payload.get("text", ""),
                    score=float(r.score),
                    doc_title=payload.get("doc_title"),
                    source_url=payload.get("source_url"),
                )
            )
        return hits


# ──────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────


_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """Return the configured VectorStore singleton."""
    global _store
    if _store is not None:
        return _store

    backend = get_settings().VECTOR_BACKEND
    if backend == "qdrant":
        _store = QdrantStore()
    else:
        _store = PgVectorStore()
    return _store


def reset_vector_store() -> None:
    """Test helper to reset the cached store (e.g., after changing settings)."""
    global _store
    _store = None
