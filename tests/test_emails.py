"""Tests for the follow-up email API.

Covers the new tenant-wide outbox endpoint (``GET /emails``):

* response shape (the SPA's CommunicationsList consumes this directly)
* status / search / date filtering
* pagination metadata
* tenant scoping — must NEVER leak rows from another tenant

We don't exercise the actual Gmail/Outlook send path here (that's
``test_email_senders.py``); we just seed ``EmailSend`` rows directly
and read them back.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


pytestmark = pytest.mark.asyncio


# ── Test app harness ─────────────────────────────────────────────────


@pytest_asyncio.fixture
async def emails_app(test_session_factory, test_tenant):
    """Mount only the emails router with a synthetic admin principal
    pinned to ``test_tenant``."""
    from backend.app.api.emails import router as emails_router
    from backend.app.auth import AuthPrincipal, get_current_principal
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
        # Re-hydrate the tenant per request so any seat/tenant mutations
        # are picked up. ``user`` is None here — we treat the test caller
        # as a programmatic admin (matches the API-key flow in prod).
        from backend.app.models import Tenant
        from sqlalchemy import select

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
        )

    app = FastAPI()
    app.include_router(emails_router, prefix="/api/v1", tags=["emails"])
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_principal] = _override_principal
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def emails_client(emails_app):
    transport = ASGITransport(app=emails_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── Seed helpers ─────────────────────────────────────────────────────


async def _seed_email_send(
    session_factory,
    *,
    tenant_id: uuid.UUID,
    to_address: str = "sarah@foo.com",
    subject: str = "Thanks for the call",
    body: str = "Following up.",
    status: str = "sent",
    interaction_id: uuid.UUID | None = None,
    sender_user_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
):
    from backend.app.models import EmailSend

    async with session_factory() as session:
        row = EmailSend(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            sender_user_id=sender_user_id,
            provider="google",
            to_address=to_address,
            cc_address=None,
            subject=subject,
            body=body,
            status=status,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        if created_at is not None:
            # SQLite ignores `server_default=func.now()` time-zone, but we
            # want a deterministic ordering in tests, so overwrite it
            # explicitly via UPDATE.
            from sqlalchemy import update

            async with session_factory() as s2:
                await s2.execute(
                    update(EmailSend)
                    .where(EmailSend.id == row.id)
                    .values(created_at=created_at)
                )
                await s2.commit()
        return row


async def _seed_other_tenant(session_factory):
    from backend.app.models import Tenant

    async with session_factory() as session:
        other = Tenant(name="Other Tenant", slug=f"other-{uuid.uuid4().hex[:6]}")
        session.add(other)
        await session.commit()
        await session.refresh(other)
        return other


# ── Tests ────────────────────────────────────────────────────────────


async def test_list_emails_returns_paginated_shape(
    emails_client, test_session_factory, test_tenant
):
    await _seed_email_send(
        test_session_factory, tenant_id=test_tenant.id, subject="A"
    )
    await _seed_email_send(
        test_session_factory, tenant_id=test_tenant.id, subject="B"
    )

    resp = await emails_client.get("/api/v1/emails")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Top-level shape — the SPA reads `items` / `total` / `limit` / `offset`.
    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    assert body["total"] == 2
    assert body["limit"] == 50  # default
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    # Item shape — must include the join fields the table renders.
    sample = body["items"][0]
    expected_keys = {
        "id",
        "interaction_id",
        "interaction_title",
        "interaction_channel",
        "sender_user_id",
        "sender_name",
        "sender_email",
        "provider",
        "to_address",
        "cc_address",
        "subject",
        "body",
        "status",
        "provider_message_id",
        "error",
        "sent_at",
        "created_at",
    }
    assert expected_keys.issubset(sample.keys())


async def test_list_emails_filters_by_status(
    emails_client, test_session_factory, test_tenant
):
    await _seed_email_send(
        test_session_factory, tenant_id=test_tenant.id, status="sent"
    )
    await _seed_email_send(
        test_session_factory, tenant_id=test_tenant.id, status="failed"
    )
    await _seed_email_send(
        test_session_factory, tenant_id=test_tenant.id, status="pending"
    )

    resp = await emails_client.get("/api/v1/emails?status=failed")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "failed"


async def test_list_emails_search_matches_recipient_or_subject(
    emails_client, test_session_factory, test_tenant
):
    await _seed_email_send(
        test_session_factory,
        tenant_id=test_tenant.id,
        to_address="sarah@foo.com",
        subject="Renewal options",
    )
    await _seed_email_send(
        test_session_factory,
        tenant_id=test_tenant.id,
        to_address="bob@bar.com",
        subject="Demo recap",
    )

    # Match by recipient.
    resp = await emails_client.get("/api/v1/emails?q=sarah")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["to_address"] == "sarah@foo.com"

    # Match by subject — case-insensitive.
    resp = await emails_client.get("/api/v1/emails?q=DEMO")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["subject"] == "Demo recap"


async def test_list_emails_pagination_respects_limit_and_offset(
    emails_client, test_session_factory, test_tenant
):
    for i in range(5):
        await _seed_email_send(
            test_session_factory,
            tenant_id=test_tenant.id,
            subject=f"msg-{i}",
        )

    resp = await emails_client.get("/api/v1/emails?limit=2&offset=0")
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["offset"] == 0

    resp = await emails_client.get("/api/v1/emails?limit=2&offset=4")
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 1
    assert body["offset"] == 4


async def test_list_emails_is_tenant_scoped(
    emails_client, test_session_factory, test_tenant
):
    """Cross-tenant isolation — the principal is pinned to test_tenant,
    so a row written under another tenant must NEVER appear."""
    other = await _seed_other_tenant(test_session_factory)

    await _seed_email_send(
        test_session_factory,
        tenant_id=test_tenant.id,
        subject="ours",
    )
    await _seed_email_send(
        test_session_factory,
        tenant_id=other.id,
        subject="theirs — DO NOT LEAK",
    )

    resp = await emails_client.get("/api/v1/emails")
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["subject"] == "ours"
    assert all(
        "DO NOT LEAK" not in item["subject"] for item in body["items"]
    )


async def test_list_emails_filters_by_date_range(
    emails_client, test_session_factory, test_tenant
):
    now = datetime.now(timezone.utc)
    await _seed_email_send(
        test_session_factory,
        tenant_id=test_tenant.id,
        subject="old",
        created_at=now - timedelta(days=10),
    )
    await _seed_email_send(
        test_session_factory,
        tenant_id=test_tenant.id,
        subject="recent",
        created_at=now - timedelta(days=1),
    )

    cutoff = (now - timedelta(days=5)).isoformat()
    resp = await emails_client.get(
        "/api/v1/emails", params={"date_from": cutoff}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1, body
    assert body["items"][0]["subject"] == "recent"
