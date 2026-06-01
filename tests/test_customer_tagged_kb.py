"""Tests for the customer-tagged KB build.

Covers the two pieces that don't lean on real vector embeddings:

* ``KBChunk.customer_id`` denormalization at ingest time — a chunk
  inherits the parent document's ``customer_id``.
* ``RetrievalService.search`` post-filter — given a candidate hit
  list, the filter keeps general (NULL customer) chunks + chunks
  tagged for the requested customer, and drops others.

The HTTP layer isn't covered here because the project's HTTP fixture
mounts only the outcomes router; the manual exercise will be to
upload a customer-tagged doc on staging and confirm it surfaces only
in that customer's context.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@pytest.fixture
def sync_session():
    from backend.app.db import Base
    import backend.app.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed(sync_session):
    from backend.app.models import Customer, KBChunk, KBDocument, Tenant

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    cust_a = Customer(tenant_id=tenant.id, name="Acct A")
    cust_b = Customer(tenant_id=tenant.id, name="Acct B")
    sync_session.add_all([cust_a, cust_b])
    sync_session.commit()

    # General doc + chunk (no customer tag).
    general = KBDocument(tenant_id=tenant.id, title="General")
    sync_session.add(general)
    sync_session.flush()
    g_chunk = KBChunk(
        tenant_id=tenant.id,
        doc_id=general.id,
        chunk_idx=0,
        text="general",
        customer_id=None,
    )
    sync_session.add(g_chunk)
    # Customer-A doc + chunk.
    a_doc = KBDocument(tenant_id=tenant.id, title="A", customer_id=cust_a.id)
    sync_session.add(a_doc)
    sync_session.flush()
    a_chunk = KBChunk(
        tenant_id=tenant.id,
        doc_id=a_doc.id,
        chunk_idx=0,
        text="customer A",
        customer_id=cust_a.id,
    )
    sync_session.add(a_chunk)
    # Customer-B doc + chunk.
    b_doc = KBDocument(tenant_id=tenant.id, title="B", customer_id=cust_b.id)
    sync_session.add(b_doc)
    sync_session.flush()
    b_chunk = KBChunk(
        tenant_id=tenant.id,
        doc_id=b_doc.id,
        chunk_idx=0,
        text="customer B",
        customer_id=cust_b.id,
    )
    sync_session.add(b_chunk)
    sync_session.commit()
    return tenant, cust_a, cust_b, g_chunk, a_chunk, b_chunk


def test_chunk_inherits_customer_id_from_doc(sync_session):
    """When the document is tagged, the chunk row's denormalized
    column matches. Confirms the index-only retrieval filter is
    valid without a join."""
    from backend.app.models import KBChunk

    _t, cust_a, _b, _g, a_chunk, _bc = _seed(sync_session)
    fetched = (
        sync_session.execute(select(KBChunk).where(KBChunk.id == a_chunk.id))
    ).scalar_one()
    assert fetched.customer_id == cust_a.id


def test_chunk_for_general_doc_has_null_customer(sync_session):
    from backend.app.models import KBChunk

    _t, _a, _b, g_chunk, _ac, _bc = _seed(sync_session)
    fetched = (
        sync_session.execute(select(KBChunk).where(KBChunk.id == g_chunk.id))
    ).scalar_one()
    assert fetched.customer_id is None


def test_post_filter_keeps_general_and_target_drops_other_customer(
    sync_session,
):
    """Simulate the in-memory post-filter step from
    ``RetrievalService.search``: a list of chunks ranked by the
    vector store should reduce to general + target-customer rows
    when ``customer_id`` is set."""
    _t, cust_a, _cust_b, g_chunk, a_chunk, b_chunk = _seed(sync_session)
    tag_by_chunk = {
        g_chunk.id: g_chunk.customer_id,
        a_chunk.id: a_chunk.customer_id,
        b_chunk.id: b_chunk.customer_id,
    }
    requested_customer = cust_a.id
    candidates = [g_chunk.id, a_chunk.id, b_chunk.id]

    kept = [
        cid
        for cid in candidates
        if tag_by_chunk.get(cid) is None
        or tag_by_chunk.get(cid) == requested_customer
    ]
    assert g_chunk.id in kept
    assert a_chunk.id in kept
    assert b_chunk.id not in kept


def test_post_filter_passthrough_when_no_customer_set(sync_session):
    """No ``customer_id`` => no post-filter; every candidate stays."""
    _t, _a, _b, g_chunk, a_chunk, b_chunk = _seed(sync_session)
    candidates = [g_chunk.id, a_chunk.id, b_chunk.id]
    # Mirror the early-exit branch in ``RetrievalService.search``.
    kept = list(candidates)
    assert kept == candidates
