"""Tests for the new ``/api/v1/manager`` router.

Spins up a minimal FastAPI app that mounts only the manager router with
auth dependencies overridden — same pattern as
``tests/db_fixtures.py:test_app``. Covers:

* Narrative endpoint reads the latest ``BusinessProfile`` joined with
  the tenant's playbook insights.
* Acknowledge / dismiss endpoints update the right columns.
* Apply dispatcher per category creates the right artifact and stamps
  the recommendation as ``applied``.
* Role gate: an agent principal gets 403 on every ``/manager/*`` route.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def engine_factory():
    from backend.app.db import Base
    import backend.app.models  # noqa: F401

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded(engine_factory):
    _, factory = engine_factory
    from backend.app.models import (
        BusinessProfile,
        Tenant,
        User,
    )

    async with factory() as session:
        tenant = Tenant(
            name="Acme",
            slug=f"acme-{uuid.uuid4().hex[:6]}",
            tenant_context={
                "playbook_insights": {
                    "what_works": ["Ask discovery questions early."],
                    "what_doesnt": ["Avoid generic pricing answers."],
                }
            },
        )
        session.add(tenant)
        await session.flush()

        manager = User(
            tenant_id=tenant.id,
            email=f"m-{uuid.uuid4().hex[:6]}@acme.test",
            name="Manager",
            role="manager",
        )
        agent = User(
            tenant_id=tenant.id,
            email=f"a-{uuid.uuid4().hex[:6]}@acme.test",
            name="Agent",
            role="agent",
        )
        session.add_all([manager, agent])
        await session.flush()

        bp = BusinessProfile(
            business_tenant_id=tenant.id,
            tenant_id=tenant.id,
            version=3,
            profile={"summary": "Sentiment is up week over week; refund volume stable."},
            top_factors=[{"label": "discovery_lift", "direction": "positive", "weight": 0.4}],
            confidence=0.72,
        )
        session.add(bp)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(manager)
        await session.refresh(agent)
        return {"tenant": tenant, "manager": manager, "agent": agent}


def _make_client(factory, tenant, principal_user, role: str):
    from fastapi import FastAPI

    from backend.app.api.manager import router as manager_router
    from backend.app.auth import (
        AuthPrincipal,
        get_current_principal,
        get_current_tenant,
    )
    from backend.app.db import get_db

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_tenant():
        return tenant

    async def _override_principal():
        return AuthPrincipal(
            tenant=tenant,
            user=principal_user,
            role=role,
            source="session",
            scopes=["*"],
        )

    app = FastAPI()
    app.include_router(manager_router, prefix="/api/v1", tags=["manager"])
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_tenant] = _override_tenant
    app.dependency_overrides[get_current_principal] = _override_principal
    return app


async def test_narrative_returns_business_profile_and_playbook(engine_factory, seeded):
    _, factory = engine_factory
    app = _make_client(factory, seeded["tenant"], seeded["manager"], "manager")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v1/manager/narrative")
    assert r.status_code == 200
    body = r.json()
    assert "Sentiment is up" in body["summary"]
    assert body["version"] == 3
    assert body["confidence"] == 0.72
    assert "what_works" in body["playbook_insights"]


async def test_role_gate_blocks_agent(engine_factory, seeded):
    _, factory = engine_factory
    app = _make_client(factory, seeded["tenant"], seeded["agent"], "agent")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v1/manager/narrative")
    assert r.status_code == 403


async def test_acknowledge_alert_stamps_columns(engine_factory, seeded):
    _, factory = engine_factory
    from backend.app.models import ManagerAlert

    async with factory() as session:
        alert = ManagerAlert(
            tenant_id=seeded["tenant"].id,
            kind="topic_spike",
            severity="high",
            title="Refund mentions jumped 6x.",
            evidence={"topic": "refund"},
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        )
        session.add(alert)
        await session.commit()
        await session.refresh(alert)
        alert_id = alert.id

    app = _make_client(factory, seeded["tenant"], seeded["manager"], "manager")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/api/v1/manager/alerts/{alert_id}/acknowledge")
    assert r.status_code == 200

    async with factory() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(ManagerAlert).where(ManagerAlert.id == alert_id)
            )
        ).scalar_one()
        assert row.acknowledged_at is not None
        assert row.acknowledged_by_user_id == seeded["manager"].id


async def test_apply_coach_rep_creates_coaching_note(engine_factory, seeded):
    _, factory = engine_factory
    from backend.app.models import CoachingNote, ManagerRecommendation

    async with factory() as session:
        rec = ManagerRecommendation(
            tenant_id=seeded["tenant"].id,
            category="coach_rep",
            title="Coach Agent on discovery questions.",
            rationale="Open-question rate dropped 18 points across recent calls.",
            evidence={"call_count": 12},
            target={"rep_user_id": str(seeded["agent"].id)},
            score=82.0,
            expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        rec_id = rec.id

    app = _make_client(factory, seeded["tenant"], seeded["manager"], "manager")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/api/v1/manager/recommendations/{rec_id}/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact_type"] == "coaching_note"

    async with factory() as session:
        from sqlalchemy import select

        note = (
            await session.execute(
                select(CoachingNote).where(CoachingNote.id == uuid.UUID(body["artifact_id"]))
            )
        ).scalar_one()
        assert note.assigned_to == seeded["agent"].id
        assert note.source_recommendation_id == rec_id

        applied = (
            await session.execute(
                select(ManagerRecommendation).where(ManagerRecommendation.id == rec_id)
            )
        ).scalar_one()
        assert applied.status == "applied"
        assert applied.applied_artifact_type == "coaching_note"


async def test_apply_promote_script_appends_playbook(engine_factory, seeded):
    _, factory = engine_factory
    from backend.app.models import ManagerRecommendation, Tenant

    async with factory() as session:
        rec = ManagerRecommendation(
            tenant_id=seeded["tenant"].id,
            category="promote_winning_script",
            title="Promote the renewal-window framing.",
            rationale="Top reps use it on every renewal call.",
            evidence={"sample_count": 8},
            target={"script_phrase": "Let's lock in your renewal window today."},
            score=70.0,
            expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        rec_id = rec.id

    app = _make_client(factory, seeded["tenant"], seeded["manager"], "manager")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/api/v1/manager/recommendations/{rec_id}/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact_type"] == "playbook_entry"

    async with factory() as session:
        from sqlalchemy import select

        tenant = (
            await session.execute(
                select(Tenant).where(Tenant.id == seeded["tenant"].id)
            )
        ).scalar_one()
        scripts = (tenant.tenant_context or {}).get("playbook", {}).get("scripts") or []
        assert any(
            "renewal window" in (s.get("phrase") or "").lower() for s in scripts
        )


async def test_alert_config_get_then_put_roundtrip(engine_factory, seeded):
    _, factory = engine_factory
    app = _make_client(factory, seeded["tenant"], seeded["manager"], "manager")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v1/manager/alert-config")
        assert r.status_code == 200
        original = r.json()
        assert original["inapp_enabled"] is True

        r2 = await c.put(
            "/api/v1/manager/alert-config",
            json={"slack_enabled": True, "slack_min_severity": "high"},
        )
        assert r2.status_code == 200
        updated = r2.json()
        assert updated["slack_enabled"] is True
        assert updated["slack_min_severity"] == "high"
