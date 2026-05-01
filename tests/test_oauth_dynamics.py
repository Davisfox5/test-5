"""Microsoft Dynamics 365 OAuth flow tests.

Covers:
- Provider listed in /oauth/providers
- Authorize requires the per-org Dynamics environment URL
- Authorize URL embeds the per-org ``.default`` resource scope
- Callback exchanges code + persists org_url on provider_config
- Userinfo lookup populates provider_user_id from WhoAmI

Same fixture shape as ``test_oauth_crm_callback`` — no Postgres or
FastAPI TestClient.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

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
async def test_dynamics_listed_certified_when_secrets_set():
    with patch.object(oauth, "_provider_setting", lambda attr: "stub"):
        resp = await oauth.oauth_providers()
    names = {p.provider for p in resp.providers}
    assert "microsoft_dynamics" in names
    dyn = next(p for p in resp.providers if p.provider == "microsoft_dynamics")
    assert dyn.certified is True


@pytest.mark.asyncio
async def test_dynamics_listed_uncertified_when_secrets_missing():
    with patch.object(oauth, "_provider_setting", lambda attr: ""):
        resp = await oauth.oauth_providers()
    dyn = next(p for p in resp.providers if p.provider == "microsoft_dynamics")
    assert dyn.certified is False


# ── Authorize URL builder ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_requires_org_url(fake_request):
    """Without org_url we 400 — Dynamics scope is per-org."""
    from fastapi import HTTPException

    class FakeRedis:
        async def setex(self, key, ttl, value):
            pass

        async def aclose(self):
            pass

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ):
        with pytest.raises(HTTPException) as exc:
            await oauth._build_provider_authorize_url(
                "microsoft_dynamics",
                fake_request,
                tenant_id=uuid.uuid4(),
                user_id=None,
            )
    assert exc.value.status_code == 400
    assert "org_url" in exc.value.detail


@pytest.mark.asyncio
async def test_authorize_url_embeds_per_org_default_scope(fake_request):
    """The Dynamics scope must include ``<org>/.default`` so AAD knows
    which resource we want a token for."""
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
            "microsoft_dynamics",
            fake_request,
            tenant_id=uuid.uuid4(),
            user_id=None,
            org_url="https://contoso.crm.dynamics.com",
        )
    parsed = urlparse(url)
    assert parsed.netloc == "login.microsoftonline.com"
    qs = parse_qs(parsed.query)
    scopes = qs["scope"][0].split(" ")
    assert "https://contoso.crm.dynamics.com/.default" in scopes
    assert "offline_access" in scopes
    # Org URL persisted on the stashed payload for the callback.
    stashed = json.loads(next(iter(state_store.values())))
    assert stashed["org_url"] == "https://contoso.crm.dynamics.com"


@pytest.mark.asyncio
async def test_authorize_url_normalizes_org_url(fake_request):
    """A bare hostname should be promoted to https + trailing slash trimmed."""
    state_store: dict = {}

    class FakeRedis:
        async def setex(self, key, ttl, value):
            state_store[key] = value

        async def aclose(self):
            pass

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ):
        await oauth._build_provider_authorize_url(
            "microsoft_dynamics",
            fake_request,
            tenant_id=uuid.uuid4(),
            user_id=None,
            org_url="contoso.crm.dynamics.com/",
        )
    stashed = json.loads(next(iter(state_store.values())))
    assert stashed["org_url"] == "https://contoso.crm.dynamics.com"


# ── Callback ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dynamics_callback_persists_org_url_and_provider_user_id(
    fake_db, fake_request
):
    tenant_id = str(uuid.uuid4())
    posts: list = []
    gets: list = []

    class FakeRedis:
        async def get(self, key):
            return json.dumps(
                {
                    "tenant_id": tenant_id,
                    "provider": "microsoft_dynamics",
                    "org_url": "https://contoso.crm.dynamics.com",
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
                    "access_token": "dyn-at",
                    "refresh_token": "dyn-rt",
                    "expires_in": 3600,
                },
            )

        async def get(self, url, headers=None):
            gets.append(url)
            # WhoAmI response shape from Dynamics 365 Web API.
            return _mock_http(
                200,
                {
                    "UserId": "11111111-2222-3333-4444-555555555555",
                    "BusinessUnitId": "bu-id",
                    "OrganizationId": "org-id",
                },
            )

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ), patch("backend.app.api.oauth.httpx.AsyncClient", lambda **kw: FakeHttpClient()):
        result = await oauth.oauth_callback(
            provider="microsoft_dynamics",
            request=fake_request,
            code="code-dyn",
            state="state-dyn",
            error=None,
            db=fake_db,
        )

    assert result == {"status": "connected", "provider": "microsoft_dynamics"}
    # Token POST went to login.microsoftonline.com common endpoint
    assert any(
        "login.microsoftonline.com/common/oauth2/v2.0/token" in url for url in posts
    )
    # WhoAmI GET went to the per-org REST endpoint
    assert any(
        "contoso.crm.dynamics.com/api/data/v9.2/WhoAmI" in url for url in gets
    )
    integ = fake_db.added[0]
    assert integ.provider == "microsoft_dynamics"
    assert decrypt_token(integ.access_token) == "dyn-at"
    assert decrypt_token(integ.refresh_token) == "dyn-rt"
    assert integ.provider_config["org_url"] == "https://contoso.crm.dynamics.com"
    assert (
        integ.provider_config["provider_user_id"]
        == "11111111-2222-3333-4444-555555555555"
    )


@pytest.mark.asyncio
async def test_dynamics_callback_handles_token_exchange_error(
    fake_db, fake_request
):
    """A 4xx from AAD surfaces as a 400 with the provider error in the
    detail string — keeps the failure observable in the SPA toast."""
    from fastapi import HTTPException

    class FakeRedis:
        async def get(self, key):
            return json.dumps(
                {
                    "tenant_id": str(uuid.uuid4()),
                    "provider": "microsoft_dynamics",
                    "org_url": "https://contoso.crm.dynamics.com",
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
            return _mock_http(400, {"error": "invalid_grant"})

    with patch.object(oauth, "_get_redis", lambda: FakeRedis()), patch.object(
        oauth, "_provider_setting", lambda attr: "stub"
    ), patch("backend.app.api.oauth.httpx.AsyncClient", lambda **kw: FakeHttpClient()):
        with pytest.raises(HTTPException) as exc:
            await oauth.oauth_callback(
                provider="microsoft_dynamics",
                request=fake_request,
                code="c",
                state="s",
                error=None,
                db=fake_db,
            )
    assert exc.value.status_code == 400
    assert "invalid_grant" in exc.value.detail
