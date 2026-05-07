"""Shared helpers for UC vendor (RingCentral / Webex / Zoom Phone) tests.

Each provider has its own ``test_uc_<vendor>.py`` exercising the full
webhook → idempotency → fetch loop end-to-end against ``respx``-fixtured
HTTP responses. This module hosts the bits they all share — fixture
loaders, a tenant/integration seeder, and a focused FastAPI test app
that mounts only the UC router.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "uc"


def load_fixture(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text())


def synthetic_mp3() -> bytes:
    """A minimal MP3 byte string: ID3 header + a single sync frame.

    The audio normalizer's ``detect_format`` recognises ID3 magic bytes
    as MP3, so this is enough to drive the format-detection branch
    without bundling a real audio file in the repo.
    """
    return (
        b"ID3\x04\x00\x00\x00\x00\x00\x0a"
        + b"\x00" * 10
        + b"\xff\xfb\x90\x64"
        + b"\x00" * 100
    )


@pytest_asyncio.fixture
async def uc_test_app(test_session_factory):
    """FastAPI app with only the UC router mounted, plus DB override."""
    from backend.app.api.uc_telephony import router as uc_router
    from backend.app.db import get_db

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(uc_router, prefix="/api/v1", tags=["uc-telephony"])
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def uc_test_client(uc_test_app):
    transport = ASGITransport(app=uc_test_app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


_SIGNING_SECRET_ENV: Dict[str, str] = {
    "ringcentral": "RINGCENTRAL_WEBHOOK_SECRET",
    "webex_calling": "WEBEX_WEBHOOK_SECRET",
    "zoom_phone": "ZOOM_PHONE_WEBHOOK_SECRET",
}


@pytest_asyncio.fixture
async def seeded_uc_integration(test_session_factory, test_tenant, monkeypatch):
    """Seed an Integration row for each UC provider on the test tenant.

    Also sets the vendor-wide signing-secret env vars so the route
    handlers (which read from env, not the Integration row) can verify
    test webhook deliveries. Per-tenant ``provider_config["webhook_secret"]``
    storage was removed — secrets are global per-vendor now.
    """
    from backend.app.models import Integration
    from backend.app.services.token_crypto import encrypt_token

    secrets = {
        "ringcentral": "rc-fixture-verification-token",
        "webex_calling": "webex-fixture-webhook-secret",
        "zoom_phone": "zoom-fixture-secret-token",
    }

    for provider, secret in secrets.items():
        monkeypatch.setenv(_SIGNING_SECRET_ENV[provider], secret)

    async with test_session_factory() as session:
        out: Dict[str, Any] = {}
        for provider, secret in secrets.items():
            integ = Integration(
                tenant_id=test_tenant.id,
                provider=provider,
                access_token=encrypt_token(
                    "fixture-access-token-" + provider
                ),
                refresh_token=encrypt_token(
                    "fixture-refresh-token-" + provider
                ),
                scopes=[],
                provider_config={},
            )
            session.add(integ)
            await session.flush()
            out[provider] = {
                "integration_id": integ.id,
                "secret": secret,
            }
        await session.commit()
        return out
