"""Tests for the on-demand email backfill endpoints.

Covers the API contract: 404 when no mailbox is connected, 202 + queued
job + worker enqueue on success, 409 (with the existing job_id) when one
is already running, 501 for providers without a backfill path yet, and
the status poll. The worker itself (``email_backfill_run``) is exercised
separately — here we assert it gets enqueued, not that it runs.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def backfill_app(test_session_factory, test_tenant, monkeypatch):
    """Focused app mounting only the backfill router.

    The Celery task's ``.delay`` is patched to a recorder so the test
    asserts enqueue without standing up a broker.
    """
    from fastapi import FastAPI

    from backend.app.api.email_backfill import router as backfill_router
    from backend.app.auth import get_current_tenant
    from backend.app.db import get_db

    enqueued: list[str] = []

    import backend.app.tasks as tasks_mod

    class _FakeTask:
        def delay(self, *args, **kwargs):
            enqueued.append(args[0] if args else None)

    monkeypatch.setattr(tasks_mod, "email_backfill_run", _FakeTask())

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_get_tenant():
        from sqlalchemy import select

        from backend.app.models import Tenant

        async with test_session_factory() as s:
            result = await s.execute(
                select(Tenant).where(Tenant.id == test_tenant.id)
            )
            return result.scalar_one()

    app = FastAPI()
    app.include_router(backfill_router, prefix="/api/v1", tags=["email-backfill"])
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_tenant] = _override_get_tenant
    app.state._enqueued = enqueued  # surfaced to tests
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def backfill_client(backfill_app):
    transport = ASGITransport(app=backfill_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed_integration(test_session_factory, tenant_id, provider="google"):
    from backend.app.models import Integration

    async with test_session_factory() as session:
        integ = Integration(
            tenant_id=tenant_id,
            provider=provider,
            access_token="enc-access",
            refresh_token="enc-refresh",
            scopes=[],
        )
        session.add(integ)
        await session.commit()
        await session.refresh(integ)
        return integ


async def test_backfill_404_when_no_integration(backfill_client):
    resp = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google", "days": 90}
    )
    assert resp.status_code == 404


async def test_backfill_starts_and_enqueues(
    backfill_client, backfill_app, test_session_factory, test_tenant
):
    await _seed_integration(test_session_factory, test_tenant.id)

    resp = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google", "days": 90}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["window_days"] == 90
    job_id = body["job_id"]

    # Worker was enqueued with this job id.
    assert backfill_app.state._enqueued == [job_id]

    # Job row persisted.
    from sqlalchemy import select

    from backend.app.models import EmailBackfillJob

    async with test_session_factory() as s:
        job = (
            await s.execute(
                select(EmailBackfillJob).where(
                    EmailBackfillJob.id == uuid.UUID(job_id)
                )
            )
        ).scalar_one()
        assert job.status == "queued"
        assert job.provider == "google"
        assert job.window_days == 90


async def test_backfill_days_capped_at_90(
    backfill_client, test_session_factory, test_tenant
):
    await _seed_integration(test_session_factory, test_tenant.id)
    resp = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google", "days": 365}
    )
    assert resp.status_code == 202
    assert resp.json()["window_days"] == 90


async def test_backfill_409_when_already_running(
    backfill_client, test_session_factory, test_tenant
):
    await _seed_integration(test_session_factory, test_tenant.id)
    first = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google"}
    )
    assert first.status_code == 202
    first_job_id = first.json()["job_id"]

    second = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google"}
    )
    assert second.status_code == 409
    # The existing job id is returned so the caller can poll it.
    assert second.json()["detail"]["job_id"] == first_job_id


async def test_backfill_microsoft_501(
    backfill_client, test_session_factory, test_tenant
):
    await _seed_integration(test_session_factory, test_tenant.id, provider="microsoft")
    resp = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "microsoft"}
    )
    assert resp.status_code == 501


async def test_backfill_status_roundtrip(
    backfill_client, test_session_factory, test_tenant
):
    await _seed_integration(test_session_factory, test_tenant.id)
    start = await backfill_client.post(
        "/api/v1/email/backfill", json={"provider": "google"}
    )
    job_id = start.json()["job_id"]

    status = await backfill_client.get(f"/api/v1/email/backfill/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["job_id"] == job_id
    assert body["status"] == "queued"
    assert body["fetched"] == 0
    assert body["ingested"] == 0
    assert body["skipped"] == 0


async def test_backfill_status_404_unknown_job(backfill_client):
    resp = await backfill_client.get(f"/api/v1/email/backfill/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_backfill_query_string():
    pytest.importorskip("googleapiclient")
    from backend.app.services.email_ingest.gmail import backfill_query

    assert backfill_query(90) == "newer_than:90d -in:chats -in:spam -in:trash"
