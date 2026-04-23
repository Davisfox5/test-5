"""Tests for the CRM OAuth callback — state validation + token exchange.

We don't use the full FastAPI TestClient (which would require Postgres).
Instead we exercise the callback handler at the module level with a
mocked httpx exchange + Redis state + async DB session.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api import oauth
from backend.app.services import token_crypto
from backend.app.services.token_crypto import decrypt_token


@pytest.fixture(autouse=True)
def _fernet_key():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    with patch.object(token_crypto, "get_settings") as gs:
        gs.return_value.TOKEN_ENCRYPTION_KEY = key
        gs.return_value.DEBUG = False
        token_crypto.reset_cache_for_tests()
        yield
    token_crypto.reset_cache_for_tests()


@pytest.fixture
def fake_db():
    """Minimal async session that captures adds + no-ops on execute."""

    class FakeResult:
        def scalar_one_or_none(self):
            return None

    class DB:
        def __init__(self) -> None:
            self.added = []

        def add(self, obj) -> None:
            self.added.append(obj)

        async def execute(self, stmt) -> FakeResult:
            return FakeResult()

        async def flush(self) -> None:
            return None

        async def delete(self, obj) -> None:
            return None

    return DB()


@pytest.fixture
def fake_request():
    """Stand-in Request with just what our helpers read."""
    req = SimpleNamespace()
    req.base_url = "http://localhost:8000/"
    return req


def _mock_http(status: int, body: dict):
    """Build an httpx-response-shaped SimpleNamespace."""
    return SimpleNamespace(
        status_code=status,
        text=json.dumps(body),
        json=lambda: body,
    )


# ── State lifecycle ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stash_and_pop_state_roundtrip_with_fake_redis():
    store: dict = {}

    class FakeRedis:
        async def setex(self, key, ttl, value):
            store[key] = value

        async def get(self, key):
            return store.get(key)

        async def delete(self, key):
            store.pop(key, None)

        async def aclose(self):
            pass

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()):
        await oauth._stash_state("abc", {"tenant_id": "t1"})
        popped = await oauth._pop_state("abc")
        assert popped == {"tenant_id": "t1"}
        # Second pop returns None (already deleted).
        assert await oauth._pop_state("abc") is None


@pytest.mark.asyncio
async def test_pop_state_missing_returns_none():
    class FakeRedis:
        async def get(self, key):
            return None

        async def aclose(self):
            pass

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()):
        assert await oauth._pop_state("missing") is None


# ── Callback: HubSpot token exchange ─────────────────────────────────


@pytest.mark.asyncio
async def test_hubspot_callback_exchanges_code_and_stores_encrypted(
    fake_db, fake_request
):
    tenant_id = str(uuid.uuid4())

    class FakeRedis:
        async def get(self, key):
            assert key == "oauth_state:state-xyz"
            return json.dumps({"tenant_id": tenant_id, "provider": "hubspot"})

        async def delete(self, key):
            pass

        async def aclose(self):
            pass

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, headers=None):
            assert "hubapi.com" in url
            assert data["code"] == "code-123"
            return _mock_http(
                200,
                {
                    "access_token": "at-123",
                    "refresh_token": "rt-456",
                    "expires_in": 3600,
                },
            )

    # Patch settings so we pass the client_id/secret guard.
    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ), patch("backend.app.api.oauth.httpx.AsyncClient", lambda **kw: FakeHttpClient()):
        result = await oauth.oauth_callback(
            provider="hubspot",
            request=fake_request,
            code="code-123",
            state="state-xyz",
            error=None,
            db=fake_db,
        )

    assert result == {"status": "connected", "provider": "hubspot"}
    # One Integration row added, with tokens encrypted at rest.
    assert len(fake_db.added) == 1
    integ = fake_db.added[0]
    assert integ.provider == "hubspot"
    assert integ.access_token != "at-123"  # encrypted
    assert decrypt_token(integ.access_token) == "at-123"
    assert decrypt_token(integ.refresh_token) == "rt-456"


@pytest.mark.asyncio
async def test_salesforce_callback_stores_instance_url_in_provider_config(
    fake_db, fake_request
):
    tenant_id = str(uuid.uuid4())

    class FakeRedis:
        async def get(self, key):
            return json.dumps({"tenant_id": tenant_id, "provider": "salesforce"})

        async def delete(self, key):
            pass

        async def aclose(self):
            pass

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, headers=None):
            return _mock_http(
                200,
                {
                    "access_token": "sf-at",
                    "refresh_token": "sf-rt",
                    "instance_url": "https://na1.salesforce.com/",
                },
            )

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ), patch("backend.app.api.oauth.httpx.AsyncClient", lambda **kw: FakeHttpClient()):
        await oauth.oauth_callback(
            provider="salesforce",
            request=fake_request,
            code="c",
            state="s",
            error=None,
            db=fake_db,
        )

    integ = fake_db.added[0]
    assert integ.provider_config["instance_url"] == "https://na1.salesforce.com"
    assert decrypt_token(integ.access_token) == "sf-at"


@pytest.mark.asyncio
async def test_pipedrive_callback_stores_api_domain(fake_db, fake_request):
    class FakeRedis:
        async def get(self, key):
            return json.dumps(
                {"tenant_id": str(uuid.uuid4()), "provider": "pipedrive"}
            )

        async def delete(self, key):
            pass

        async def aclose(self):
            pass

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, headers=None):
            return _mock_http(
                200,
                {
                    "access_token": "pd-at",
                    "refresh_token": "pd-rt",
                    "api_domain": "https://foo.pipedrive.com/",
                    "expires_in": 3600,
                },
            )

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ), patch("backend.app.api.oauth.httpx.AsyncClient", lambda **kw: FakeHttpClient()):
        await oauth.oauth_callback(
            provider="pipedrive",
            request=fake_request,
            code="c",
            state="s",
            error=None,
            db=fake_db,
        )

    integ = fake_db.added[0]
    assert integ.provider_config["api_domain"] == "https://foo.pipedrive.com"


# ── Error paths ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_callback_rejects_unknown_provider(fake_db, fake_request):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await oauth.oauth_callback(
            provider="mystery",
            request=fake_request,
            code="c",
            state="s",
            error=None,
            db=fake_db,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_callback_rejects_missing_code(fake_db, fake_request):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await oauth.oauth_callback(
            provider="hubspot",
            request=fake_request,
            code=None,
            state="s",
            error=None,
            db=fake_db,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_callback_rejects_expired_state(fake_db, fake_request):
    from fastapi import HTTPException

    class FakeRedis:
        async def get(self, key):
            return None

        async def aclose(self):
            pass

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()):
        with pytest.raises(HTTPException) as exc:
            await oauth.oauth_callback(
                provider="hubspot",
                request=fake_request,
                code="c",
                state="expired",
                error=None,
                db=fake_db,
            )
    assert exc.value.status_code == 400
    assert "state" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_callback_surfaces_provider_error(fake_db, fake_request):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await oauth.oauth_callback(
            provider="hubspot",
            request=fake_request,
            code="c",
            state="s",
            error="user_denied",
            db=fake_db,
        )
    assert exc.value.status_code == 400
    assert "user_denied" in exc.value.detail
