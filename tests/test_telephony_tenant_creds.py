"""Tests for per-tenant Twilio credential resolution.

Verifies that ``_twilio_creds`` prefers the tenant's Integration row
over the env-var fallback, and that the decrypted auth_token comes
through cleanly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app.api.telephony import TwilioCreds, _twilio_creds
from backend.app.services.token_crypto import encrypt_token


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeDB:
    """Return a canned Integration row when the telephony code queries
    for one. Anything else (not expected) returns None."""

    def __init__(self, integ):
        self._integ = integ

    async def execute(self, stmt):
        return _FakeResult(self._integ)


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch):
    """Set up a deterministic Fernet key so encrypt/decrypt round-trips."""
    from cryptography.fernet import Fernet
    from backend.app.services import token_crypto

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        token_crypto,
        "get_settings",
        lambda: SimpleNamespace(
            TOKEN_ENCRYPTION_KEY=key, DEBUG=False
        ),
    )
    token_crypto.reset_cache_for_tests()
    yield
    token_crypto.reset_cache_for_tests()


@pytest.mark.asyncio
async def test_creds_prefer_tenant_integration_over_env(monkeypatch):
    """When both an Integration row and env vars exist, the Integration
    wins — that's the whole point of per-tenant credentials."""
    from backend.app.api import telephony as tele

    monkeypatch.setattr(
        tele,
        "get_settings",
        lambda: SimpleNamespace(
            TWILIO_ACCOUNT_SID="env-sid",
            TWILIO_AUTH_TOKEN="env-token",
        ),
    )

    integ = SimpleNamespace(
        provider_config={"account_sid": "AC_tenant_sid"},
        access_token=encrypt_token("tenant-auth-token"),
    )
    db = FakeDB(integ)
    creds = await _twilio_creds(uuid.uuid4(), db)

    assert creds.account_sid == "AC_tenant_sid"
    assert creds.auth_token == "tenant-auth-token"
    assert creds.source == "tenant"


@pytest.mark.asyncio
async def test_creds_fall_back_to_env_when_no_integration(monkeypatch):
    from backend.app.api import telephony as tele

    monkeypatch.setattr(
        tele,
        "get_settings",
        lambda: SimpleNamespace(
            TWILIO_ACCOUNT_SID="env-sid",
            TWILIO_AUTH_TOKEN="env-token",
        ),
    )
    db = FakeDB(None)
    creds = await _twilio_creds(uuid.uuid4(), db)

    assert creds.account_sid == "env-sid"
    assert creds.auth_token == "env-token"
    assert creds.source == "env"


@pytest.mark.asyncio
async def test_creds_fall_back_when_integration_incomplete(monkeypatch):
    """An Integration row that's missing the account_sid shouldn't win —
    the env fallback is safer than hitting Twilio with a blank SID."""
    from backend.app.api import telephony as tele

    monkeypatch.setattr(
        tele,
        "get_settings",
        lambda: SimpleNamespace(
            TWILIO_ACCOUNT_SID="env-sid",
            TWILIO_AUTH_TOKEN="env-token",
        ),
    )

    incomplete = SimpleNamespace(
        provider_config={},  # no account_sid
        access_token=encrypt_token("some-token"),
    )
    db = FakeDB(incomplete)
    creds = await _twilio_creds(uuid.uuid4(), db)
    assert creds.source == "env"


@pytest.mark.asyncio
async def test_creds_returns_empty_when_nothing_configured(monkeypatch):
    """No Integration + no env → empty creds. Callers decide whether
    that's fatal (outbound dial: 503) or accepted (dev signature bypass)."""
    from backend.app.api import telephony as tele

    monkeypatch.setattr(
        tele,
        "get_settings",
        lambda: SimpleNamespace(TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN=""),
    )
    db = FakeDB(None)
    creds = await _twilio_creds(uuid.uuid4(), db)
    assert creds.account_sid == ""
    assert creds.auth_token == ""
    assert creds.source == "env"


@pytest.mark.asyncio
async def test_creds_preserve_decryption_across_integration(monkeypatch):
    """Regression guard: if decrypt_token is ever swapped out for a
    stricter implementation, tenant creds should still come back as
    plaintext strings (not Fernet ciphertext)."""
    from backend.app.api import telephony as tele

    monkeypatch.setattr(
        tele,
        "get_settings",
        lambda: SimpleNamespace(
            TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN=""
        ),
    )

    plaintext = "super-secret-twilio-auth"
    integ = SimpleNamespace(
        provider_config={"account_sid": "AC_x"},
        access_token=encrypt_token(plaintext),
    )
    db = FakeDB(integ)
    creds = await _twilio_creds(uuid.uuid4(), db)
    assert creds.auth_token == plaintext  # decrypted, not ciphertext
    assert not creds.auth_token.startswith("gAAAAA")
