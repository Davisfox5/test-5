"""Tests for the OAuth disconnect path — upstream revocation + mail purge.

The revoke endpoint must do three things when a mailbox is disconnected
(Google Limited Use / GDPR erase-on-disconnect):

1. Revoke the grant at the provider so the token can't be reused.
2. Delete the stored token row.
3. Purge every email Interaction (+ attachments) ingested from that mailbox.

We exercise the helpers at the module level with a fake async session and a
mocked httpx client — no Postgres required, matching test_oauth_crm_callback.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api import oauth


class _ScalarResult:
    """Mimics the slice of the SQLAlchemy Result API our code touches."""

    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeDB:
    """Async session that returns queued scalar results per execute() and
    records every statement it was handed for assertions."""

    def __init__(self, scalar_results):
        # scalar_results: list of row-lists, returned in execute() order.
        self._queue = list(scalar_results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = self._queue.pop(0) if self._queue else []
        return _ScalarResult(rows)


# ── _revoke_google_token ────────────────────────────────


def _patch_httpx(post_mock):
    client = AsyncMock()
    client.post = post_mock
    ctx = AsyncMock()
    ctx.__aenter__.return_value = client
    ctx.__aexit__.return_value = False
    return patch.object(oauth.httpx, "AsyncClient", return_value=ctx)


@pytest.mark.asyncio
async def test_revoke_google_token_posts_to_google():
    post = AsyncMock(return_value=SimpleNamespace(status_code=200))
    with _patch_httpx(post):
        await oauth._revoke_google_token("refresh-abc")
    post.assert_awaited_once()
    args, kwargs = post.call_args
    assert args[0] == oauth.GOOGLE_REVOKE_URL
    assert kwargs["data"] == {"token": "refresh-abc"}


@pytest.mark.asyncio
async def test_revoke_google_token_tolerates_400_and_no_token():
    # Already-invalid token → Google returns 400; must not raise.
    post = AsyncMock(return_value=SimpleNamespace(status_code=400))
    with _patch_httpx(post):
        await oauth._revoke_google_token("stale")
    post.assert_awaited_once()

    # Empty token → no network call at all.
    post2 = AsyncMock()
    with _patch_httpx(post2):
        await oauth._revoke_google_token(None)
    post2.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_google_token_swallows_network_error():
    post = AsyncMock(side_effect=RuntimeError("boom"))
    with _patch_httpx(post):
        # Local disconnect must still proceed — helper never raises.
        await oauth._revoke_google_token("tok")


# ── _purge_ingested_email ───────────────────────────────


@pytest.mark.asyncio
async def test_purge_unknown_provider_is_noop():
    db = FakeDB([])
    purged = await oauth._purge_ingested_email(
        db, tenant_id=uuid.uuid4(), provider="hubspot"
    )
    assert purged == 0
    assert db.statements == []  # never even queried


@pytest.mark.asyncio
async def test_purge_no_email_returns_zero():
    db = FakeDB([[]])  # interaction-id query returns nothing
    purged = await oauth._purge_ingested_email(
        db, tenant_id=uuid.uuid4(), provider="google"
    )
    assert purged == 0
    # Only the lookup ran — no attachment / delete statements.
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_purge_deletes_interactions_and_attachments():
    iid1, iid2 = uuid.uuid4(), uuid.uuid4()
    # execute order: interaction ids, attachment s3 keys, update, delete.
    db = FakeDB([[iid1, iid2], ["tenants/x/att/k1", "tenants/x/att/k2"]])

    fake_store = MagicMock()
    with patch(
        "backend.app.services.attachment_store.get_store",
        return_value=fake_store,
    ):
        purged = await oauth._purge_ingested_email(
            db, tenant_id=uuid.uuid4(), provider="google"
        )

    assert purged == 2
    # Both attachment objects were deleted from object storage.
    assert fake_store.delete.call_count == 2
    fake_store.delete.assert_any_call("tenants/x/att/k1")
    # Four statements: select ids, select keys, null live_sessions, delete.
    assert len(db.statements) == 4
