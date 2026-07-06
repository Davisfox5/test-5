"""Cross-tenant isolation for the vector store (4c).

The Qdrant backend is one shared collection with a payload filter — the
choke point equivalent of RLS for Postgres. These tests prove, against a
real (in-process) Qdrant engine:

- a tenant's search NEVER returns another tenant's chunks, even when the
  other tenant's vector is the nearest neighbour;
- tenant offboarding purges exactly that tenant's points;

…and that the doc-level ``retrieve()`` wrapper re-filters by tenant in
SQL even if the vector layer were to misbehave (belt and suspenders).
"""

import uuid

import pytest

pytest.importorskip("qdrant_client")

from backend.app.services.kb.vector_store import ChunkRecord, QdrantStore  # noqa: E402


def _make_store(dim: int = 4) -> QdrantStore:
    from qdrant_client import AsyncQdrantClient

    store = QdrantStore.__new__(QdrantStore)
    store._client = AsyncQdrantClient(":memory:")
    store._dim = dim
    return store


def _chunk(tenant_id, vector, text):
    return ChunkRecord(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        doc_id=uuid.uuid4(),
        chunk_idx=0,
        text=text,
        embedding=vector,
    )


@pytest.mark.asyncio
async def test_search_never_crosses_tenants():
    store = _make_store()
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    vec = [1.0, 0.0, 0.0, 0.0]
    await store.upsert(None, [_chunk(tenant_a, [0.0, 1.0, 0.0, 0.0], "a-doc")])
    await store.upsert(None, [_chunk(tenant_b, vec, "b-doc")])

    # Query with tenant B's EXACT vector, as tenant A: the nearest
    # neighbour globally is B's chunk — the filter must hide it.
    hits = await store.search(None, tenant_id=tenant_a, query_embedding=vec, k=5)
    assert [h.text for h in hits] == ["a-doc"]

    hits_b = await store.search(None, tenant_id=tenant_b, query_embedding=vec, k=5)
    assert [h.text for h in hits_b] == ["b-doc"]


@pytest.mark.asyncio
async def test_purge_tenant_removes_only_that_tenant():
    store = _make_store()
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    vec = [0.5, 0.5, 0.0, 0.0]

    await store.upsert(None, [_chunk(tenant_a, vec, "a-doc")])
    await store.upsert(None, [_chunk(tenant_b, vec, "b-doc")])

    await store.purge_tenant(None, tenant_a)

    assert await store.search(None, tenant_id=tenant_a, query_embedding=vec, k=5) == []
    remaining = await store.search(None, tenant_id=tenant_b, query_embedding=vec, k=5)
    assert [h.text for h in remaining] == ["b-doc"]


@pytest.mark.asyncio
async def test_doc_retrieve_refilters_by_tenant_in_sql(
    test_session, test_tenant, monkeypatch
):
    """Even if the vector layer returned a foreign doc id, retrieve()'s SQL
    re-fetch must drop it."""
    from backend.app.models import KBDocument, Tenant
    from backend.app.services import kb_document_retrieval as kdr
    from backend.app.services.kb.vector_store import SearchHit

    other = Tenant(name="Other", slug="other-{0}".format(uuid.uuid4().hex[:8]))
    test_session.add(other)
    await test_session.flush()

    mine = KBDocument(tenant_id=test_tenant.id, title="mine", content="pricing guide")
    theirs = KBDocument(tenant_id=other.id, title="theirs", content="pricing guide")
    test_session.add_all([mine, theirs])
    await test_session.commit()

    def _hit(doc, score):
        return SearchHit(
            chunk_id=uuid.uuid4(),
            doc_id=doc.id,
            chunk_idx=0,
            text=doc.content,
            score=score,
        )

    class _FakeRetrieval:
        async def search(self, db, tenant_id, query, k=3, **kwargs):
            # Misbehaving vector layer: leaks the other tenant's doc first.
            return [_hit(theirs, 0.99), _hit(mine, 0.42)]

    monkeypatch.setattr(
        "backend.app.services.kb.retrieval.RetrievalService",
        lambda *a, **kw: _FakeRetrieval(),
    )

    ranked = await kdr.retrieve(test_session, test_tenant.id, "pricing", k=5)
    assert [doc.title for doc, _score in ranked] == ["mine"]
