"""Zoho CRM OAuth flow tests.

Covers:
- Provider listed in /oauth/providers
- Authorize URL builds with the correct regional accounts host
- Callback exchanges code for token + persists region on the integration row
- Userinfo lookup populates provider_user_id

Mocks the httpx exchanges + Redis state + async DB session — same
pattern as ``test_oauth_crm_callback`` (no Postgres / FastAPI TestClient).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

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
    req = SimpleNamespace()
    req.base_url = "http://localhost:8000/"
    return req


def _mock_http(status: int, body: dict):
    return SimpleNamespace(
        status_code=status,
        text=json.dumps(body),
        json=lambda: body,
    )


# ── /oauth/providers listing ──────────────────────────────────────


@pytest.mark.asyncio
async def test_zoho_listed_in_providers_with_runtime_certification():
    """When secrets are configured the provider lights up as certified."""
    with patch.object(oauth, "_provider_setting", lambda attr: "stub"):
        resp = await oauth.oauth_providers()
    names = {p.provider for p in resp.providers}
    assert "zoho" in names
    zoho = next(p for p in resp.providers if p.provider == "zoho")
    assert zoho.certified is True


@pytest.mark.asyncio
async def test_zoho_listed_uncertified_when_secrets_missing():
    """Surfaced for SPA discovery but flagged uncertified until env is set."""
    with patch.object(oauth, "_provider_setting", lambda attr: ""):
        resp = await oauth.oauth_providers()
    zoho = next(p for p in resp.providers if p.provider == "zoho")
    assert zoho.certified is False


# ── Authorize URL builder ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_url_uses_us_host_by_default(fake_request):
    """Default region maps to accounts.zoho.com."""
    state_store: dict = {}

    class FakeRedis:
        async def setex(self, key, ttl, value):
            state_store[key] = value

        async def aclose(self):
            pass

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ):
        url = await oauth._build_provider_authorize_url(
            "zoho",
            fake_request,
            tenant_id=uuid.uuid4(),
            user_id=None,
        )
    assert url.startswith("https://accounts.zoho.com/oauth/v2/auth?")
    # Stashed payload carries the region for the callback to consult.
    stashed = json.loads(next(iter(state_store.values())))
    assert stashed["region"] == "us"
    # Required OAuth params are present.
    assert "scope=" in url
    assert "ZohoCRM.modules.contacts.READ" in url
    assert "access_type=offline" in url


@pytest.mark.asyncio
async def test_authorize_url_uses_eu_host_when_region_eu(fake_request):
    state_store: dict = {}

    class FakeRedis:
        async def setex(self, key, ttl, value):
            state_store[key] = value

        async def aclose(self):
            pass

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ):
        url = await oauth._build_provider_authorize_url(
            "zoho",
            fake_request,
            tenant_id=uuid.uuid4(),
            user_id=None,
            region="eu",
        )
    assert url.startswith("https://accounts.zoho.eu/oauth/v2/auth?")
    stashed = json.loads(next(iter(state_store.values())))
    assert stashed["region"] == "eu"


@pytest.mark.asyncio
async def test_authorize_url_falls_back_to_us_for_unknown_region(fake_request):
    """Defensive: a typo'd region shouldn't 500 — fall back to US."""

    class FakeRedis:
        async def setex(self, key, ttl, value):
            pass

        async def aclose(self):
            pass

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ):
        url = await oauth._build_provider_authorize_url(
            "zoho",
            fake_request,
            tenant_id=uuid.uuid4(),
            user_id=None,
            region="atlantis",
        )
    assert url.startswith("https://accounts.zoho.com/oauth/v2/auth?")


# ── Callback ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zoho_callback_exchanges_code_against_regional_host(
    fake_db, fake_request
):
    """Token exchange must hit the EU host when the state says ``eu``."""
    tenant_id = str(uuid.uuid4())
    posts: list = []
    gets: list = []

    class FakeRedis:
        async def get(self, key):
            return json.dumps(
                {
                    "tenant_id": tenant_id,
                    "provider": "zoho",
                    "region": "eu",
                }
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
            posts.append(url)
            return _mock_http(
                200,
                {
                    "access_token": "zoho-at",
                    "refresh_token": "zoho-rt",
                    "expires_in": 3600,
                    "api_domain": "https://www.zohoapis.eu",
                },
            )

        async def get(self, url, headers=None):
            gets.append(url)
            return _mock_http(200, {"ZUID": "9000123"})

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ), patch("backend.app.api.oauth.httpx.AsyncClient", lambda **kw: FakeHttpClient()):
        result = await oauth.oauth_callback(
            provider="zoho",
            request=fake_request,
            code="code-zoho",
            state="state-zoho",
            error=None,
            db=fake_db,
        )

    assert result == {"status": "connected", "provider": "zoho"}
    # Token POST went to the EU host
    assert any("accounts.zoho.eu/oauth/v2/token" in url for url in posts)
    # Userinfo GET went to the EU host
    assert any("accounts.zoho.eu/oauth/user/info" in url for url in gets)
    # Integration row persisted with the region + provider_user_id
    integ = fake_db.added[0]
    assert integ.provider == "zoho"
    assert decrypt_token(integ.access_token) == "zoho-at"
    assert decrypt_token(integ.refresh_token) == "zoho-rt"
    assert integ.provider_config["region"] == "eu"
    assert integ.provider_config["api_domain"] == "https://www.zohoapis.eu"
    assert integ.provider_config["provider_user_id"] == "9000123"


@pytest.mark.asyncio
async def test_zoho_callback_succeeds_without_userinfo(fake_db, fake_request):
    """Userinfo failures don't block the connect."""
    tenant_id = str(uuid.uuid4())

    class FakeRedis:
        async def get(self, key):
            return json.dumps(
                {"tenant_id": tenant_id, "provider": "zoho", "region": "us"}
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
                {"access_token": "z", "refresh_token": "r", "expires_in": 3600},
            )

        async def get(self, url, headers=None):
            # Userinfo call returns 500 — should be swallowed.
            return _mock_http(500, {"error": "boom"})

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ), patch("backend.app.api.oauth.httpx.AsyncClient", lambda **kw: FakeHttpClient()):
        await oauth.oauth_callback(
            provider="zoho",
            request=fake_request,
            code="c",
            state="s",
            error=None,
            db=fake_db,
        )

    integ = fake_db.added[0]
    assert "provider_user_id" not in integ.provider_config
    assert integ.provider_config["region"] == "us"
