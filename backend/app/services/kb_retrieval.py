"""Retrieval for the KB — Qdrant when configured, keyword fallback otherwise.

The reply drafter calls :func:`retrieve` with a free-form query (the
email thread's most recent message + subject).  We return a ranked list
of ``(KBDocument, score)`` tuples; the caller decides how many to paste
into the Sonnet prompt.

Embeddings are pluggable via :class:`EmbeddingClient`.  If no provider
is configured we degrade gracefully to a PostgreSQL keyword ranker.
That fallback is deterministic, tenant-scoped, and — critically — never
introduces cross-tenant leakage.
"""

from __future__ import annotations

import logging
import math
import re
from typing import List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import KBDocument

logger = logging.getLogger(__name__)

QDRANT_COLLECTION_PREFIX = "kb_tenant_"

# ── Embedding client ────────────────────────────────────


class EmbeddingClient:
    """Lazy, optional embedding provider.

    Currently supports Voyage AI (Anthropic's recommended partner).  Add
    more providers by extending :meth:`embed_texts`.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def available(self) -> bool:
        return bool(getattr(self._settings, "VOYAGE_API_KEY", ""))

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not self.available:
            return []
        import httpx

        resp = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            json={"input": texts, "model": "voyage-3", "input_type": "document"},
            headers={
                "Authorization": f"Bearer {self._settings.VOYAGE_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return [row["embedding"] for row in resp.json()["data"]]


# ── Qdrant client wrapper ───────────────────────────────


class QdrantStore:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._settings.QDRANT_URL)

    def _get(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(
                url=self._settings.QDRANT_URL,
                api_key=self._settings.QDRANT_API_KEY or None,
                timeout=10,
            )
        return self._client

    def collection(self, tenant_id) -> str:
        return f"{QDRANT_COLLECTION_PREFIX}{tenant_id}"

    def ensure_collection(self, tenant_id, vector_size: int = 1024) -> None:
        from qdrant_client.models import Distance, VectorParams

        client = self._get()
        name = self.collection(tenant_id)
        existing = {c.name for c in client.get_collections().collections}
        if name not in existing:
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )

    def upsert(self, tenant_id, doc_id: str, vector: List[float], payload: dict) -> None:
        from qdrant_client.models import PointStruct

        self.ensure_collection(tenant_id, vector_size=len(vector))
        self._get().upsert(
            collection_name=self.collection(tenant_id),
            points=[PointStruct(id=doc_id, vector=vector, payload=payload)],
        )

    def query(self, tenant_id, vector: List[float], k: int) -> List[Tuple[str, float]]:
        hits = self._get().search(
            collection_name=self.collection(tenant_id), query_vector=vector, limit=k
        )
        return [(str(h.id), float(h.score)) for h in hits]

    def delete(self, tenant_id, doc_id: str) -> None:
        self._get().delete(
            collection_name=self.collection(tenant_id),
            points_selector=[doc_id],
        )


# ── Public API ──────────────────────────────────────────


_embed = EmbeddingClient()
_store = QdrantStore()


async def index_document(db: AsyncSession, doc: KBDocument) -> None:
    """Upsert a single KB doc into Qdrant if vectors are available.

    Non-fatal: logs and returns on any failure, so a dead Qdrant never
    blocks a KB write.
    """
    if not (_embed.available and _store.available):
        return
    content = f"{doc.title or ''}\n\n{doc.content or ''}"[:8000]
    try:
        vectors = _embed.embed_texts([content])
        if not vectors:
            return
        _store.upsert(
            doc.tenant_id,
            str(doc.id),
            vectors[0],
            {"title": doc.title or "", "tags": doc.tags or []},
        )
        doc.qdrant_point_id = str(doc.id)
    except Exception:
        logger.exception("KB Qdrant index failed for doc %s (non-fatal)", doc.id)


async def delete_from_index(tenant_id, doc_id) -> None:
    if not _store.available:
        return
    try:
        _store.delete(tenant_id, str(doc_id))
    except Exception:
        logger.exception("KB Qdrant delete failed for doc %s (non-fatal)", doc_id)


async def retrieve(
    db: AsyncSession, tenant_id, query: str, k: int = 5
) -> List[Tuple[KBDocument, float]]:
    """Return ranked KB docs for a query, tenant-scoped.

    Tries Qdrant vector search first.  Falls back to a keyword ranker
    that walks SQL candidates and scores them by term overlap — good
    enough to surface the right doc on small corpora and never
    hallucinates a cross-tenant hit.
    """
    # Vector path
    if _embed.available and _store.available:
        try:
            vector = _embed.embed_texts([query])[0]
            ranked = _store.query(tenant_id, vector, k=k)
            if ranked:
                import uuid as _uuid

                ids = [_uuid.UUID(pid) for pid, _ in ranked]
                rows = (
                    await db.execute(
                        select(KBDocument).where(
                            KBDocument.tenant_id == tenant_id,
                            KBDocument.id.in_(ids),
                        )
                    )
                ).scalars().all()
                by_id = {str(r.id): r for r in rows}
                return [
                    (by_id[pid], score)
                    for pid, score in ranked
                    if pid in by_id
                ]
        except Exception:
            logger.exception("Qdrant retrieval failed; falling back to keyword ranker")

    return await _keyword_ranker(db, tenant_id, query, k)


# ── Keyword fallback ────────────────────────────────────


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


async def _keyword_ranker(
    db: AsyncSession, tenant_id, query: str, k: int
) -> List[Tuple[KBDocument, float]]:
    """Very small TF ranker — pulls a larger candidate set via ILIKE on
    the top-N most distinctive query tokens and rescores in Python.
    """
    tokens = _tokenize(query)[:8]
    if not tokens:
        rows = (await db.execute(
            select(KBDocument)
            .where(KBDocument.tenant_id == tenant_id)
            .order_by(KBDocument.last_synced_at.desc().nullslast())
            .limit(k)
        )).scalars().all()
        return [(r, 0.0) for r in rows]

    clauses = []
    for tok in tokens:
        pattern = f"%{tok}%"
        clauses.append(KBDocument.content.ilike(pattern))
        clauses.append(KBDocument.title.ilike(pattern))

    candidates = (
        await db.execute(
            select(KBDocument)
            .where(KBDocument.tenant_id == tenant_id, or_(*clauses))
            .limit(50)
        )
    ).scalars().all()

    scored: List[Tuple[KBDocument, float]] = []
    for doc in candidates:
        haystack = _tokenize((doc.title or "") + " " + (doc.content or ""))
        if not haystack:
            continue
        counts: dict[str, int] = {}
        for t in haystack:
            counts[t] = counts.get(t, 0) + 1
        score = 0.0
        for q in tokens:
            score += math.log(1 + counts.get(q, 0))
        if score > 0:
            scored.append((doc, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]
