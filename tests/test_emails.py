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


# ── send-follow-up failure contract ──────────────────────────────────
#
# Consumers (Flex) must be able to detect a provider failure
# programmatically: a 2xx response always means the provider accepted
# the message; provider failures are 401 (auth) / 502 (send), and the
# ``failed`` EmailSend row survives the error response (it used to be
# rolled back with the raised HTTPException).


async def _seed_interaction_and_integration(session_factory, tenant_id):
    from backend.app.models import Integration, Interaction

    async with session_factory() as session:
        interaction = Interaction(tenant_id=tenant_id, channel="voice")
        integ = Integration(
            tenant_id=tenant_id,
            provider="google",
            access_token=None,
            refresh_token=None,
        )
        session.add(interaction)
        session.add(integ)
        await session.commit()
        await session.refresh(interaction)
        return interaction


class _FakeSender:
    """Stands in for GmailSender/OutlookSender via a patched _build_sender."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.closed = False

    async def send(self, **kwargs):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def close(self):
        self.closed = True


def _patch_sender(monkeypatch, outcome) -> _FakeSender:
    from backend.app.api import emails as emails_module

    sender = _FakeSender(outcome)
    monkeypatch.setattr(
        emails_module, "_build_sender", lambda integ, principal_email_hint: sender
    )
    return sender


async def _get_send_rows(session_factory, tenant_id):
    from sqlalchemy import select

    from backend.app.models import EmailSend

    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(EmailSend).where(EmailSend.tenant_id == tenant_id)
                )
            ).scalars().all()
        )


async def test_send_follow_up_provider_failure_returns_502_with_failed_row(
    emails_client, test_session_factory, test_tenant, monkeypatch
):
    from backend.app.services.email.base import EmailSendError

    interaction = await _seed_interaction_and_integration(
        test_session_factory, test_tenant.id
    )
    sender = _patch_sender(monkeypatch, EmailSendError("gmail 500: backend error"))

    resp = await emails_client.post(
        f"/api/v1/interactions/{interaction.id}/send-follow-up",
        json={"to": "sarah@foo.com", "subject": "Hi", "body": "Following up."},
    )

    # Never a 2xx "sent" on provider failure.
    assert resp.status_code == 502, resp.text
    assert "gmail 500" in resp.json()["detail"]
    assert sender.closed is True

    # The failed row was committed despite the raised HTTPException.
    rows = await _get_send_rows(test_session_factory, test_tenant.id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "gmail 500" in (rows[0].error or "")


async def test_send_follow_up_auth_failure_returns_401_with_failed_row(
    emails_client, test_session_factory, test_tenant, monkeypatch
):
    from backend.app.services.email.base import EmailAuthError

    interaction = await _seed_interaction_and_integration(
        test_session_factory, test_tenant.id
    )
    _patch_sender(monkeypatch, EmailAuthError("token revoked"))

    resp = await emails_client.post(
        f"/api/v1/interactions/{interaction.id}/send-follow-up",
        json={"to": "sarah@foo.com", "subject": "Hi", "body": "Following up."},
    )

    assert resp.status_code == 401, resp.text
    rows = await _get_send_rows(test_session_factory, test_tenant.id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert (rows[0].error or "").startswith("auth:")


async def test_send_follow_up_unexpected_error_returns_502_not_2xx(
    emails_client, test_session_factory, test_tenant, monkeypatch
):
    interaction = await _seed_interaction_and_integration(
        test_session_factory, test_tenant.id
    )
    _patch_sender(monkeypatch, RuntimeError("connection reset mid-flight"))

    resp = await emails_client.post(
        f"/api/v1/interactions/{interaction.id}/send-follow-up",
        json={"to": "sarah@foo.com", "subject": "Hi", "body": "Following up."},
    )

    assert resp.status_code == 502, resp.text
    rows = await _get_send_rows(test_session_factory, test_tenant.id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "RuntimeError" in (rows[0].error or "")


async def test_send_follow_up_success_returns_201_sent(
    emails_client, test_session_factory, test_tenant, monkeypatch
):
    from types import SimpleNamespace

    interaction = await _seed_interaction_and_integration(
        test_session_factory, test_tenant.id
    )
    _patch_sender(
        monkeypatch,
        SimpleNamespace(provider_message_id="prov-123", message_id="msg-123"),
    )

    resp = await emails_client.post(
        f"/api/v1/interactions/{interaction.id}/send-follow-up",
        json={"to": "sarah@foo.com", "subject": "Hi", "body": "Following up."},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "sent"
    assert body["error"] is None
    assert body["provider_message_id"] == "prov-123"
