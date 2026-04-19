"""Tests for per-user auth: password hashing, JWT sessions, require_role gate."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException, Request
from jose import jwt

from backend.app import auth as auth_mod
from backend.app.auth import (
    AuthPrincipal,
    hash_password,
    issue_session_token,
    require_role,
    verify_password,
    _decode_session_token,
    _jwt_secret_reset_for_tests,
    _ROLE_RANK,
)


# ── Password hashing ──────────────────────────────────────────────────


def test_password_hash_roundtrip():
    h = hash_password("hunter2")
    assert h.startswith("$2b$")
    assert verify_password("hunter2", h) is True
    assert verify_password("wrong", h) is False


def test_verify_password_handles_none_and_empty():
    assert verify_password("", "irrelevant") is False
    assert verify_password("pw", None) is False
    # Malformed hash — doesn't crash.
    assert verify_password("pw", "not-a-bcrypt-hash") is False


def test_empty_password_raises_on_hash():
    with pytest.raises(ValueError):
        hash_password("")


# ── JWT session ───────────────────────────────────────────────────────


@pytest.fixture
def fake_user():
    u = SimpleNamespace()
    u.id = uuid.uuid4()
    u.tenant_id = uuid.uuid4()
    u.role = "admin"
    return u


@pytest.fixture(autouse=True)
def _stable_jwt_secret(monkeypatch):
    """Force a stable secret + non-DEBUG behaviour for deterministic tests."""
    settings = SimpleNamespace(
        SESSION_JWT_SECRET="test-secret-abcdefghijklmnop",
        SESSION_JWT_TTL_HOURS=12,
        DEBUG=False,
    )
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)
    _jwt_secret_reset_for_tests()
    yield
    _jwt_secret_reset_for_tests()


def test_issue_token_is_decodable(fake_user):
    tok = issue_session_token(fake_user)
    payload = _decode_session_token(tok)
    assert payload is not None
    assert payload["sub"] == str(fake_user.id)
    assert payload["tid"] == str(fake_user.tenant_id)
    assert payload["role"] == "admin"


def test_expired_token_is_rejected(fake_user):
    # Hand-craft an already-expired JWT with the same secret.
    payload = {
        "sub": str(fake_user.id),
        "tid": str(fake_user.tenant_id),
        "role": "admin",
        "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
    }
    tok = jwt.encode(payload, "test-secret-abcdefghijklmnop", algorithm="HS256")
    assert _decode_session_token(tok) is None


def test_tampered_token_is_rejected(fake_user):
    tok = issue_session_token(fake_user)
    # Flip a character in the signature portion.
    tampered = tok[:-4] + ("AAAA" if tok[-4:] != "AAAA" else "BBBB")
    assert _decode_session_token(tampered) is None


def test_missing_secret_in_production_raises(monkeypatch):
    settings = SimpleNamespace(
        SESSION_JWT_SECRET="", SESSION_JWT_TTL_HOURS=12, DEBUG=False
    )
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)
    _jwt_secret_reset_for_tests()
    try:
        with pytest.raises(RuntimeError):
            issue_session_token(SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="agent"))
    finally:
        _jwt_secret_reset_for_tests()


def test_debug_mode_uses_ephemeral_secret(monkeypatch, fake_user):
    settings = SimpleNamespace(
        SESSION_JWT_SECRET="", SESSION_JWT_TTL_HOURS=1, DEBUG=True
    )
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)
    _jwt_secret_reset_for_tests()
    try:
        tok = issue_session_token(fake_user)
        assert _decode_session_token(tok) is not None
    finally:
        _jwt_secret_reset_for_tests()


# ── Role gate ─────────────────────────────────────────────────────────


def _make_principal(role: str) -> AuthPrincipal:
    return AuthPrincipal(
        tenant=SimpleNamespace(id=uuid.uuid4()),
        user=SimpleNamespace(id=uuid.uuid4()),
        role=role,
        source="session",
    )


def test_role_rank_ordering():
    # Explicit sanity: admin > manager > agent.
    assert _ROLE_RANK["admin"] > _ROLE_RANK["manager"] > _ROLE_RANK["agent"]


@pytest.mark.asyncio
async def test_require_role_admin_accepts_admin():
    dep = require_role("admin")
    principal = _make_principal("admin")
    result = await dep(principal=principal)
    assert result is principal


@pytest.mark.asyncio
async def test_require_role_admin_rejects_manager_and_agent():
    dep = require_role("admin")
    for bad in ("manager", "agent"):
        with pytest.raises(HTTPException) as exc:
            await dep(principal=_make_principal(bad))
        assert exc.value.status_code == 403
        assert "admin" in exc.value.detail


@pytest.mark.asyncio
async def test_require_role_manager_accepts_manager_and_admin():
    dep = require_role("manager")
    for role in ("admin", "manager"):
        principal = _make_principal(role)
        out = await dep(principal=principal)
        assert out is principal


@pytest.mark.asyncio
async def test_require_role_manager_rejects_agent():
    dep = require_role("manager")
    with pytest.raises(HTTPException) as exc:
        await dep(principal=_make_principal("agent"))
    assert exc.value.status_code == 403


def test_require_role_unknown_role_raises_at_construct():
    with pytest.raises(ValueError):
        require_role("superuser")  # not in the allowed set


# ── Principal resolver — precedence + source tagging ─────────────────


@pytest.mark.asyncio
async def test_session_jwt_takes_precedence_over_api_key(fake_user, monkeypatch):
    """If both a valid session token and a valid API key would match, the
    session path wins (it's tried first)."""
    tok = issue_session_token(fake_user)

    # Build a Request with that Authorization header.
    scope = {"type": "http", "headers": [(b"authorization", f"Bearer {tok}".encode())]}
    request = Request(scope)

    tenant_obj = SimpleNamespace(id=fake_user.tenant_id)
    user_obj = SimpleNamespace(
        id=fake_user.id,
        tenant_id=fake_user.tenant_id,
        tenant=tenant_obj,
        role="admin",
        is_active=True,
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return user_obj

    class FakeDB:
        async def execute(self, stmt):
            return FakeResult()

    principal = await auth_mod._principal_from_session_jwt(request, FakeDB())
    assert principal is not None
    assert principal.source == "session"
    assert principal.role == "admin"
    assert principal.user is user_obj


@pytest.mark.asyncio
async def test_api_key_path_builds_synthetic_admin():
    """An API-key caller resolves to a principal with user=None, role=admin,
    source=api_key. That's how resellers/integrations get tenant-admin scope."""
    api_key = "csk_" + "A" * 48
    scope = {"type": "http", "headers": [(b"authorization", f"Bearer {api_key}".encode())]}
    request = Request(scope)

    tenant_obj = SimpleNamespace(id=uuid.uuid4())
    api_key_row = SimpleNamespace(
        tenant=tenant_obj,
        expires_at=None,
        last_used_at=None,
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return api_key_row

    class FakeDB:
        async def execute(self, stmt):
            return FakeResult()

    principal = await auth_mod._principal_from_api_key(request, FakeDB())
    assert principal is not None
    assert principal.source == "api_key"
    assert principal.role == "admin"
    assert principal.user is None
    assert principal.tenant is tenant_obj


# ── Login rate of lookups (no-leak on bad email) ─────────────────────


@pytest.mark.asyncio
async def test_login_same_error_for_bad_email_and_bad_password():
    """The login endpoint must return an identical 401 for unknown emails
    and wrong passwords so attackers can't enumerate users."""
    from backend.app.api.auth_session import LoginIn, login

    class FakeResult:
        def __init__(self, u): self.u = u
        def scalar_one_or_none(self): return self.u

    # Case 1: user doesn't exist.
    class NoneDB:
        async def execute(self, stmt): return FakeResult(None)

    # Case 2: user exists, wrong password.
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="real@acme.com",
        password_hash=hash_password("actual-password"),
        is_active=True,
        last_login_at=None,
        name=None,
        role="agent",
        created_at=datetime.now(timezone.utc),
    )

    class ExistingDB:
        async def execute(self, stmt): return FakeResult(existing)

    body = LoginIn(email="real@acme.com", password="not-the-actual-password")

    # Fake request is unused by the login handler.
    request = Request({"type": "http", "headers": []})

    for db in (NoneDB(), ExistingDB()):
        with pytest.raises(HTTPException) as exc:
            await login(body, request, db)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid credentials"
