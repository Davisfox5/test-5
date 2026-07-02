"""Tests for the email backfill API (``/email/backfill``).

Covers the job-handle contract the Flex console (and any other API-key
consumer) polls against:

* 400 when no mailbox integration is connected for the provider
* 202 + job handle when one is, with the Celery enqueue stubbed
* idempotent re-POST while a job is queued/running (same job_id back)
* status GET returns counters and is tenant-scoped (404 across tenants)
* window validation (days > 90 → 422)
* stale-job recovery: a 'running' job whose heartbeat went silent is
  superseded (marked error) and a fresh job is enqueued
* a live 'running' job with a fresh heartbeat still dedupes re-POSTs
* a live job tied to a previously connected integration → 409

The provider fetch + ingest pipeline itself isn't exercised here — the
fetchers are thin wrappers over Gmail/Graph list APIs and ``ingest_email``
has its own coverage; we stub the Celery task and drive the job row
directly.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


pytestmark = pytest.mark.asyncio


# ── Test app harness ─────────────────────────────────────────────────


@pytest_asyncio.fixture
async def backfill_app(test_session_factory, test_tenant):
    """Mount only the backfill router with an all-scope API-key principal
    pinned to ``test_tenant``."""
    from backend.app.api.email_backfill import router as backfill_router
    from backend.app.auth import (
        AuthPrincipal,
        get_current_principal,
        get_current_tenant,
    )
    from backend.app.db import get_db

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_principal() -> AuthPrincipal:
        from sqlalchemy import select

        from backend.app.models import Tenant

        async with test_session_factory() as s:
            result = await s.execute(
                select(Tenant).where(Tenant.id == test_tenant.id)
            )
            tenant = result.scalar_one()
        return AuthPrincipal(
            tenant=tenant,
            user=None,
            role="admin",
            source="api_key",
            scopes=["*"],
        )

    async def _override_tenant():
        # get_current_tenant calls get_current_principal directly (not via
        # Depends), so it must be overridden on its own.
        return (await _override_principal()).tenant

    app = FastAPI()
    app.include_router(backfill_router, prefix="/api/v1", tags=["email-backfill"])
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_principal] = _override_principal
    app.dependency_overrides[get_current_tenant] = _override_tenant
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def backfill_client(backfill_app):
    transport = ASGITransport(app=backfill_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def gmail_integration(test_session_factory, test_tenant):
    """Seed a connected google integration for the test tenant."""
    from backend.app.models import Integration

    async with test_session_factory() as session:
        integ = Integration(
            tenant_id=test_tenant.id,
            provider="google",
            access_token="enc",
            refresh_token="enc",
        )
        session.add(integ)
        await session.commit()
        await session.refresh(integ)
        return integ


@pytest.fixture
def stub_enqueue(monkeypatch):
    """Replace the Celery task's ``delay`` with a recorder."""
    import backend.app.tasks as tasks_mod

    calls: list[str] = []

    class _Stub:
        @staticmethod
        def delay(job_id: str) -> None:
            calls.append(job_id)

    monkeypatch.setattr(tasks_mod, "email_backfill_run", _Stub)
    return calls


# ── Tests ────────────────────────────────────────────────────────────


async def test_post_without_integration_is_400(backfill_client):
    resp = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google", "days": 90}
    )
    assert resp.status_code == 400
    assert "mailbox" in resp.json()["detail"].lower()


async def test_post_creates_job_and_enqueues(
    backfill_client, gmail_integration, stub_enqueue
):
    resp = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google", "days": 30}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["window_days"] == 30
    assert stub_enqueue == [body["job_id"]]


async def test_repost_returns_in_flight_job(
    backfill_client, gmail_integration, stub_enqueue
):
    first = (
        await backfill_client.post(
            "/api/v1/email/backfill", json={"provider": "google"}
        )
    ).json()
    second = (
        await backfill_client.post(
            "/api/v1/email/backfill", json={"provider": "google"}
        )
    ).json()
    assert second["job_id"] == first["job_id"]
    # Only the first POST enqueued work.
    assert stub_enqueue == [first["job_id"]]


async def test_status_roundtrip_and_counters(
    backfill_client, gmail_integration, stub_enqueue, test_session_factory
):
    job_id = (
        await backfill_client.post(
            "/api/v1/email/backfill", json={"provider": "google"}
        )
    ).json()["job_id"]

    # Simulate the worker having made progress.
    from sqlalchemy import select

    from backend.app.models import EmailBackfillJob

    async with test_session_factory() as session:
        job = (
            await session.execute(
                select(EmailBackfillJob).where(
                    EmailBackfillJob.id == uuid.UUID(job_id)
                )
            )
        ).scalar_one()
        job.status = "done"
        job.fetched = 12
        job.ingested = 9
        job.skipped = 3
        await session.commit()

    resp = await backfill_client.get(f"/api/v1/email/backfill/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert (body["fetched"], body["ingested"], body["skipped"]) == (12, 9, 3)


async def test_status_is_tenant_scoped(backfill_client):
    resp = await backfill_client.get(f"/api/v1/email/backfill/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_window_capped_at_90_days(backfill_client, gmail_integration):
    resp = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google", "days": 120}
    )
    assert resp.status_code == 422


async def _set_job(test_session_factory, job_id, **attrs):
    from sqlalchemy import select

    from backend.app.models import EmailBackfillJob

    async with test_session_factory() as session:
        job = (
            await session.execute(
                select(EmailBackfillJob).where(
                    EmailBackfillJob.id == uuid.UUID(str(job_id))
                )
            )
        ).scalar_one()
        for key, value in attrs.items():
            setattr(job, key, value)
        await session.commit()


async def _get_job(test_session_factory, job_id):
    from sqlalchemy import select

    from backend.app.models import EmailBackfillJob

    async with test_session_factory() as session:
        return (
            await session.execute(
                select(EmailBackfillJob).where(
                    EmailBackfillJob.id == uuid.UUID(str(job_id))
                )
            )
        ).scalar_one()


async def test_stale_running_job_is_superseded(
    backfill_client, gmail_integration, stub_enqueue, test_session_factory
):
    """A 'running' job whose heartbeat went silent must not lock the
    tenant out — a re-POST supersedes it and starts a fresh job."""
    from datetime import datetime, timedelta, timezone

    first = (
        await backfill_client.post(
            "/api/v1/email/backfill", json={"provider": "google"}
        )
    ).json()
    await _set_job(
        test_session_factory,
        first["job_id"],
        status="running",
        heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    second = (
        await backfill_client.post(
            "/api/v1/email/backfill", json={"provider": "google"}
        )
    ).json()
    assert second["job_id"] != first["job_id"]
    assert stub_enqueue == [first["job_id"], second["job_id"]]

    old = await _get_job(test_session_factory, first["job_id"])
    assert old.status == "error"
    assert "superseded" in (old.error or "").lower()


async def test_live_running_job_still_dedupes(
    backfill_client, gmail_integration, stub_enqueue, test_session_factory
):
    """A running job with a fresh heartbeat is genuinely in-flight —
    re-POSTs get the same handle and no second enqueue happens."""
    from datetime import datetime, timezone

    first = (
        await backfill_client.post(
            "/api/v1/email/backfill", json={"provider": "google"}
        )
    ).json()
    await _set_job(
        test_session_factory,
        first["job_id"],
        status="running",
        heartbeat_at=datetime.now(timezone.utc),
    )

    second = (
        await backfill_client.post(
            "/api/v1/email/backfill", json={"provider": "google"}
        )
    ).json()
    assert second["job_id"] == first["job_id"]
    assert stub_enqueue == [first["job_id"]]


async def test_live_job_on_old_integration_is_409(
    backfill_client, gmail_integration, stub_enqueue, test_session_factory, test_tenant
):
    """Reconnecting a mailbox creates a new Integration row. While a
    live job is still sweeping the OLD integration, starting a second
    sweep of the same mailbox is refused (409) instead of risking
    duplicate imports; once the old job's heartbeat goes stale it gets
    superseded instead (covered above)."""
    from datetime import datetime, timedelta, timezone

    from backend.app.models import Integration

    first = (
        await backfill_client.post(
            "/api/v1/email/backfill", json={"provider": "google"}
        )
    ).json()
    await _set_job(
        test_session_factory,
        first["job_id"],
        status="running",
        heartbeat_at=datetime.now(timezone.utc),
    )

    # Reconnect: a newer google integration for the same tenant.
    async with test_session_factory() as session:
        session.add(
            Integration(
                tenant_id=test_tenant.id,
                provider="google",
                access_token="enc2",
                refresh_token="enc2",
                created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
        )
        await session.commit()

    resp = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google"}
    )
    assert resp.status_code == 409
    assert stub_enqueue == [first["job_id"]]
