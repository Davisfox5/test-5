"""Tests for ``GET /coaching/sessions``.

Two concerns the manager+ list view absolutely cannot get wrong:

1. **Tenant scoping** — a manager in tenant A must never see rows that
   belong to tenant B, even if the count or paging would otherwise
   implicate them.
2. **Pagination** — ``limit`` / ``offset`` query params must walk a
   stable, newest-first ordering so a manager scrolling past page 1
   keeps seeing strictly older sessions.

The endpoint is gated on ``require_role("manager")`` which in turn
depends on ``get_current_principal``. The shared ``test_app`` fixture
overrides ``get_current_tenant`` only — for this suite we additionally
stub ``get_current_principal`` so we can flip the principal between
two seeded tenants without going through the JWT layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.app.auth import AuthPrincipal


PREFIX = "/api/v1"


def _principal_for(tenant, role: str = "manager") -> AuthPrincipal:
    return AuthPrincipal(
        tenant=tenant,
        user=None,
        role=role,
        source="session",
    )


@pytest_asyncio.fixture
async def coaching_app(test_session_factory, test_tenant):
    """Mount only the coaching router, with auth deps stubbed.

    The shared ``test_app`` is too narrowly scoped (outcomes + ws-tickets
    only) to host the coaching router, and it doesn't override
    ``get_current_principal`` which is what ``require_role`` reaches for.
    Building a focused app here keeps both suites independent.
    """
    from fastapi import FastAPI

    from backend.app.auth import get_current_principal, get_current_tenant
    from backend.app.db import get_db
    from backend.app.api.coaching import router as coaching_router

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    state: dict = {"tenant": test_tenant, "role": "manager"}

    async def _override_get_tenant():
        return state["tenant"]

    async def _override_get_principal():
        return _principal_for(state["tenant"], role=state["role"])

    app = FastAPI()
    app.include_router(coaching_router, prefix=PREFIX, tags=["coaching"])
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_tenant] = _override_get_tenant
    app.dependency_overrides[get_current_principal] = _override_get_principal

    # Expose the mutable principal-state dict so tests can flip role
    # or active tenant between calls without rebuilding the app.
    app.state.test_principal_state = state  # type: ignore[attr-defined]
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def coaching_client(coaching_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=coaching_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def second_tenant(test_session_factory):
    """Seed a second tenant + agent so cross-tenant scoping is testable."""
    from backend.app.models import Tenant, User

    async with test_session_factory() as s:
        tenant = Tenant(name="Other Tenant", slug=f"o-{uuid.uuid4().hex[:8]}")
        s.add(tenant)
        await s.commit()
        await s.refresh(tenant)

        agent = User(
            tenant_id=tenant.id,
            email=f"other-{uuid.uuid4().hex[:6]}@example.com",
            role="agent",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return {"tenant": tenant, "agent": agent}


async def _seed_session(
    test_session_factory,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    started_at: datetime,
    status: str = "active",
    source: str = "live",
):
    from backend.app.models import LiveSession

    async with test_session_factory() as s:
        sess = LiveSession(
            tenant_id=tenant_id,
            agent_id=agent_id,
            source=source,
            status=status,
            started_at=started_at,
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        return sess


async def _seed_agent(test_session_factory, tenant_id, name="Agent A"):
    from backend.app.models import User

    async with test_session_factory() as s:
        agent = User(
            tenant_id=tenant_id,
            email=f"a-{uuid.uuid4().hex[:6]}@example.com",
            name=name,
            role="agent",
        )
        s.add(agent)
        await s.commit()
        await s.refresh(agent)
        return agent


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lists_only_current_tenant_sessions(
    coaching_client, test_session_factory, test_tenant, second_tenant
):
    """Sessions in tenant B must not bleed into tenant A's response."""
    agent_a = await _seed_agent(test_session_factory, test_tenant.id, "Alice")
    base = datetime.now(timezone.utc)
    own = await _seed_session(
        test_session_factory,
        test_tenant.id,
        agent_a.id,
        started_at=base,
    )
    # Foreign-tenant session that *must not* surface.
    foreign = await _seed_session(
        test_session_factory,
        second_tenant["tenant"].id,
        second_tenant["agent"].id,
        started_at=base + timedelta(seconds=10),
    )

    resp = await coaching_client.get(f"{PREFIX}/coaching/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    ids = {row["id"] for row in body["items"]}
    assert str(own.id) in ids
    assert str(foreign.id) not in ids
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_pagination_walks_newest_first(
    coaching_client, test_session_factory, test_tenant
):
    """Five sessions; ``limit=2&offset=2`` returns the 3rd–4th newest."""
    agent = await _seed_agent(test_session_factory, test_tenant.id, "Pager")
    base = datetime.now(timezone.utc)
    sessions = []
    for i in range(5):
        # i=0 is oldest, i=4 is newest. We expect newest-first ordering.
        sess = await _seed_session(
            test_session_factory,
            test_tenant.id,
            agent.id,
            started_at=base + timedelta(seconds=i),
        )
        sessions.append(sess)

    # Page 1: two newest (i=4, i=3)
    resp = await coaching_client.get(
        f"{PREFIX}/coaching/sessions", params={"limit": 2, "offset": 0}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["items"][0]["id"] == str(sessions[4].id)
    assert body["items"][1]["id"] == str(sessions[3].id)

    # Page 2: i=2, i=1
    resp = await coaching_client.get(
        f"{PREFIX}/coaching/sessions", params={"limit": 2, "offset": 2}
    )
    body = resp.json()
    assert [r["id"] for r in body["items"]] == [
        str(sessions[2].id),
        str(sessions[1].id),
    ]

    # Page 3: just the oldest left
    resp = await coaching_client.get(
        f"{PREFIX}/coaching/sessions", params={"limit": 2, "offset": 4}
    )
    body = resp.json()
    assert [r["id"] for r in body["items"]] == [str(sessions[0].id)]


@pytest.mark.asyncio
async def test_agent_role_is_forbidden(coaching_app, coaching_client):
    """``require_role("manager")`` must 403 a plain agent."""
    coaching_app.state.test_principal_state["role"] = "agent"
    resp = await coaching_client.get(f"{PREFIX}/coaching/sessions")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_includes_agent_name_and_duration(
    coaching_client, test_session_factory, test_tenant
):
    """Joined agent name + computed duration land on the row."""
    agent = await _seed_agent(test_session_factory, test_tenant.id, "Joined")
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    sess = await _seed_session(
        test_session_factory,
        test_tenant.id,
        agent.id,
        started_at=started,
    )
    # Patch in an ``ended_at`` so duration is computable.
    from backend.app.models import LiveSession

    async with test_session_factory() as s:
        row = await s.get(LiveSession, sess.id)
        row.ended_at = started + timedelta(seconds=300)
        row.status = "completed"
        await s.commit()

    resp = await coaching_client.get(f"{PREFIX}/coaching/sessions")
    assert resp.status_code == 200
    body = resp.json()
    target = next(r for r in body["items"] if r["id"] == str(sess.id))
    assert target["agent_name"] == "Joined"
    assert target["duration_seconds"] == 300
    assert target["status"] == "completed"
