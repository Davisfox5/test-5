"""Postgres full-text search tests for interaction transcript search.

These need a REAL Postgres — the ``search_vector`` generated column, the
GIN index, and ``ts_headline``/``websearch_to_tsquery`` don't exist in
SQLite. They build the schema (``create_all``), apply the SAME FTS DDL the
Alembic migration ships (via ``backend.app.search_ddl``), seed two tenants,
and prove:

- a query matches the tenant's own transcript and returns a highlighted
  excerpt;
- results are scoped to the requesting tenant (no cross-tenant leakage),
  independent of RLS — ``SearchService.search`` filters ``tenant_id``
  explicitly;
- structured filters (channel) narrow results;
- an empty query short-circuits to ``[]``.

Skipped automatically when no Postgres is reachable (mirrors
``tests/test_rls_isolation.py``).
"""

import os
import socket
from urllib.parse import urlparse

import pytest

TEST_POSTGRES_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql://linda_owner:test@localhost:55432/linda_test",
)


def _postgres_reachable() -> bool:
    parsed = urlparse(TEST_POSTGRES_URL)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 5432), timeout=2
        ):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="no Postgres reachable at TEST_POSTGRES_URL — FTS tests need a real Postgres",
)


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="module")
def fts_database():
    """Fresh schema + FTS DDL + two seeded tenants for the module.

    Returns (tenant_a_id, tenant_b_id, interaction_a_id, interaction_b_id).
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from backend.app.db import Base
    import backend.app.models  # noqa: F401 — registers every mapped class
    from backend.app.models import Interaction, Tenant
    from backend.app.search_ddl import create_statements

    owner = create_engine(TEST_POSTGRES_URL, isolation_level="AUTOCOMMIT")

    with owner.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    Base.metadata.create_all(owner)

    # The generated column + GIN index the migration ships.
    with owner.connect() as conn:
        for stmt in create_statements():
            conn.execute(text(stmt))

    factory = sessionmaker(bind=owner, expire_on_commit=False)
    with factory() as session:
        tenant_a = Tenant(name="FTS A", slug="fts-a")
        tenant_b = Tenant(name="FTS B", slug="fts-b")
        session.add_all([tenant_a, tenant_b])
        session.flush()

        inter_a = Interaction(
            tenant_id=tenant_a.id,
            channel="voice",
            raw_text="The customer asked about a refund for the damaged shipment.",
            insights={"summary": "Refund requested", "topics": ["refund", "shipping"]},
        )
        inter_b = Interaction(
            tenant_id=tenant_b.id,
            channel="email",
            raw_text="We walked through the onboarding and training schedule.",
            insights={"summary": "Onboarding call", "topics": ["onboarding"]},
        )
        session.add_all([inter_a, inter_b])
        session.commit()
        ids = (tenant_a.id, tenant_b.id, inter_a.id, inter_b.id)

    owner.dispose()
    return ids


async def _search(tenant_id, query, **kwargs):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.app.services.search_service import SearchService

    engine = create_async_engine(_async_url(TEST_POSTGRES_URL))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await SearchService().search(
                db=session, tenant_id=str(tenant_id), query=query, **kwargs
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_match_returns_highlighted_hit(fts_database):
    tenant_a, _tenant_b, inter_a, _inter_b = fts_database

    hits = await _search(tenant_a, "refund")

    assert len(hits) == 1
    hit = hits[0]
    assert hit["interaction_id"] == str(inter_a)
    assert hit["summary"] == "Refund requested"
    assert hit["channel"] == "voice"
    assert hit["score"] is not None and hit["score"] > 0
    # ts_headline wraps the matched term.
    assert hit["highlights"], "expected a highlighted excerpt"
    assert "<em>" in hit["highlights"][0]


@pytest.mark.asyncio
async def test_search_is_tenant_scoped(fts_database):
    tenant_a, tenant_b, _inter_a, _inter_b = fts_database

    # Tenant B's transcript has no "refund" term — and even a term that
    # existed for A must not leak across the explicit tenant filter.
    assert await _search(tenant_b, "refund") == []
    # Tenant A must not see tenant B's "onboarding" transcript.
    assert await _search(tenant_a, "onboarding") == []
    # Tenant B finds its own content.
    b_hits = await _search(tenant_b, "onboarding")
    assert len(b_hits) == 1


@pytest.mark.asyncio
async def test_topics_are_searchable(fts_database):
    tenant_a, _tenant_b, inter_a, _inter_b = fts_database
    # "shipping" appears only in tenant A's topics (weight C), not the body.
    hits = await _search(tenant_a, "shipping")
    assert [h["interaction_id"] for h in hits] == [str(inter_a)]


@pytest.mark.asyncio
async def test_channel_filter_narrows_results(fts_database):
    tenant_a, _tenant_b, _inter_a, _inter_b = fts_database
    # Tenant A's matching interaction is on the voice channel.
    assert await _search(tenant_a, "refund", channel="email") == []
    assert len(await _search(tenant_a, "refund", channel="voice")) == 1


@pytest.mark.asyncio
async def test_empty_query_returns_empty(fts_database):
    tenant_a, _tenant_b, _inter_a, _inter_b = fts_database
    assert await _search(tenant_a, "   ") == []
