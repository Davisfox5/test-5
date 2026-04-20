"""Integration test: document → chunker → embedder → VectorStore → retrieval.

Uses an in-memory FakeVectorStore and a deterministic FakeEmbedder so the
test doesn't require Postgres+pgvector or Voyage. Exercises the full flow
that ships real answers to agents on live calls.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence
from types import SimpleNamespace

import pytest

from backend.app.services.kb.ingest import ingest_document
from backend.app.services.kb.retrieval import RetrievalService
from backend.app.services.kb.vector_store import ChunkRecord, SearchHit


# ───── Fakes ────────────────────────────────────────────────────


class FakeEmbedder:
    """Maps text → a deterministic vector built from character counts.

    Gives us similarity that tracks keyword overlap without needing a real
    embedding model. Not production-quality — just enough that a query about
    "pricing" beats an unrelated chunk about "holidays".
    """

    dim = 32

    def _vec(self, text: str) -> List[float]:
        text = text.lower()
        v = [0.0] * self.dim
        for ch in text:
            v[ord(ch) % self.dim] += 1.0
        # Normalize so cosine similarity is well-defined.
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    async def embed(self, inputs: Sequence[str], input_type: str = "document") -> List[List[float]]:
        return [self._vec(t) for t in inputs]


class FakeVectorStore:
    """Pure-Python vector store used for tests. Ranks by cosine similarity."""

    def __init__(self) -> None:
        self.records: Dict[uuid.UUID, ChunkRecord] = {}

    async def upsert(self, db, chunks: Sequence[ChunkRecord]) -> None:  # noqa: ARG002
        for c in chunks:
            self.records[c.id] = c

    async def delete_doc(self, db, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> None:  # noqa: ARG002
        gone = [cid for cid, rec in self.records.items() if rec.doc_id == doc_id]
        for cid in gone:
            del self.records[cid]

    async def search(
        self,
        db,  # noqa: ARG002
        tenant_id: uuid.UUID,
        query_embedding: Sequence[float],
        k: int = 5,
        exclude_chunk_ids: Optional[Sequence[uuid.UUID]] = None,
    ) -> List[SearchHit]:
        excluded = set(exclude_chunk_ids or [])
        scored: List[SearchHit] = []
        for rec in self.records.values():
            if rec.tenant_id != tenant_id:
                continue
            if rec.id in excluded:
                continue
            score = _cosine(query_embedding, rec.embedding)
            scored.append(
                SearchHit(
                    chunk_id=rec.id,
                    doc_id=rec.doc_id,
                    chunk_idx=rec.chunk_idx,
                    text=rec.text,
                    score=score,
                    doc_title=rec.doc_title,
                    source_url=rec.source_url,
                )
            )
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5 or 1.0
    nb = sum(x * x for x in b) ** 0.5 or 1.0
    return dot / (na * nb)


class FakeSession:
    """Stand-in AsyncSession that swallows the ORM calls ingest uses."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def execute(self, *args, **kwargs):
        # ingest_document calls db.execute(KBChunk.__table__.delete().where(...))
        # — we can just return a no-op mapping.
        return SimpleNamespace(mappings=lambda: [], scalars=lambda: SimpleNamespace(all=lambda: []), scalar_one_or_none=lambda: None)


# ───── Tests ────────────────────────────────────────────────────


def _make_doc(tenant_id: uuid.UUID, title: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title=title,
        content=content,
        source_url=None,
        content_hash=None,
        embedded_at=None,
    )


@pytest.mark.asyncio
async def test_full_ingest_to_retrieval_roundtrip():
    tenant_id = uuid.uuid4()
    db = FakeSession()
    embedder = FakeEmbedder()
    store = FakeVectorStore()

    # Ingest two docs: one on pricing, one unrelated.
    pricing = _make_doc(
        tenant_id,
        "Pricing Playbook",
        "Our pro tier is $99 per month. Annual contracts receive a 20% discount.",
    )
    holidays = _make_doc(
        tenant_id,
        "Holiday Schedule",
        "The office is closed on Thanksgiving, Christmas Eve, and New Year's Day.",
    )
    n1 = await ingest_document(db, pricing, embedder=embedder, store=store)
    n2 = await ingest_document(db, holidays, embedder=embedder, store=store)
    assert n1 >= 1 and n2 >= 1

    # Retrieval should rank the pricing doc above the holiday doc for a
    # pricing query.
    service = RetrievalService(embedder=embedder, store=store)
    hits = await service.search(db, tenant_id, "how much does the pro tier cost?", k=2)
    assert len(hits) >= 1
    top = hits[0]
    assert top.doc_title == "Pricing Playbook"
    assert "pro tier" in top.text.lower()


@pytest.mark.asyncio
async def test_retrieval_is_tenant_scoped():
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    db = FakeSession()
    embedder = FakeEmbedder()
    store = FakeVectorStore()

    await ingest_document(
        db,
        _make_doc(tenant_a, "A's playbook", "Secret sauce: always lead with ROI."),
        embedder=embedder,
        store=store,
    )
    await ingest_document(
        db,
        _make_doc(tenant_b, "B's playbook", "Competitor knowledge: alpha beta gamma."),
        embedder=embedder,
        store=store,
    )

    svc = RetrievalService(embedder=embedder, store=store)
    # Tenant A shouldn't see Tenant B's chunks, period.
    hits_a = await svc.search(db, tenant_a, "competitor knowledge", k=5)
    assert all(h.doc_title != "B's playbook" for h in hits_a)


@pytest.mark.asyncio
async def test_exclude_chunk_ids_suppresses_results():
    tenant_id = uuid.uuid4()
    db = FakeSession()
    embedder = FakeEmbedder()
    store = FakeVectorStore()

    await ingest_document(
        db,
        _make_doc(
            tenant_id,
            "Pricing Playbook",
            "Pro tier is $99/mo. Enterprise is custom pricing.",
        ),
        embedder=embedder,
        store=store,
    )

    svc = RetrievalService(embedder=embedder, store=store)
    all_hits = await svc.search(db, tenant_id, "pricing", k=5)
    assert all_hits
    first_chunk = all_hits[0].chunk_id

    filtered = await svc.search(
        db, tenant_id, "pricing", k=5, exclude_chunk_ids=[first_chunk]
    )
    assert all(h.chunk_id != first_chunk for h in filtered)


@pytest.mark.asyncio
async def test_reingest_replaces_prior_chunks():
    tenant_id = uuid.uuid4()
    db = FakeSession()
    embedder = FakeEmbedder()
    store = FakeVectorStore()

    doc = _make_doc(
        tenant_id,
        "Pricing",
        "Pro tier is $49 per month.",
    )
    await ingest_document(db, doc, embedder=embedder, store=store)
    first_count = len(store.records)
    assert first_count >= 1

    # Change content and re-ingest — the old chunks should be gone.
    doc.content = "Pro tier is now $79 per month (price increase 2026)."
    doc.content_hash = None
    doc.embedded_at = None
    await ingest_document(db, doc, embedder=embedder, store=store, force=True)

    # No chunk should reference the old price.
    for rec in store.records.values():
        assert "$49" not in rec.text
    # At least one chunk mentions the new price.
    assert any("$79" in rec.text for rec in store.records.values())
