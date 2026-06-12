"""Tests for API key scope enforcement.

Covers:

* Scope validation on create / update (unknown scope → 422).
* Missing scope on a write endpoint → 403.
* ``"*"`` scope satisfies every check.
* Session-JWT principals bypass scope enforcement.
* Scope persists across regenerations.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from backend.app import auth as auth_mod
from backend.app.auth import (
    API_KEY_SCOPES,
    AuthPrincipal,
    require_scope,
    validate_scopes,
)


# ── validate_scopes ───────────────────────────────────────────────────


def test_validate_scopes_accepts_canonical_set():
    out = validate_scopes(["interactions:read", "kb:write"])
    assert out == ["interactions:read", "kb:write"]


def test_validate_scopes_dedupes_and_strips():
    out = validate_scopes([" kb:read ", "kb:read", "interactions:read"])
    assert out == ["kb:read", "interactions:read"]


def test_validate_scopes_collapses_wildcard():
    """``*`` subsumes everything else — keep only the wildcard."""
    out = validate_scopes(["interactions:write", "*", "kb:read"])
    assert out == ["*"]


def test_validate_scopes_rejects_unknown():
    with pytest.raises(ValueError) as exc:
        validate_scopes(["interactions:write", "no_such:scope"])
    assert "no_such:scope" in str(exc.value)


def test_validate_scopes_rejects_non_list():
    with pytest.raises(ValueError):
        validate_scopes("kb:read")  # type: ignore[arg-type]


def test_validate_scopes_drops_blank_strings():
    assert validate_scopes(["", "kb:read", "  "]) == ["kb:read"]


# ── require_scope dependency ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_scope_missing_raises_403():
    """An API-key principal without the scope gets 403 ``missing scope: …``."""
    tenant = SimpleNamespace(id=uuid.uuid4())
    principal = AuthPrincipal(
        tenant=tenant, user=None, role="admin", source="api_key", scopes=["kb:read"]
    )
    dep = require_scope("interactions:write")

    with pytest.raises(HTTPException) as exc:
        await dep(principal=principal)  # type: ignore[arg-type]
    assert exc.value.status_code == 403
    assert exc.value.detail == "missing scope: interactions:write"


@pytest.mark.asyncio
async def test_require_scope_present_returns_principal():
    tenant = SimpleNamespace(id=uuid.uuid4())
    principal = AuthPrincipal(
        tenant=tenant,
        user=None,
        role="admin",
        source="api_key",
        scopes=["interactions:write", "kb:read"],
    )
    dep = require_scope("interactions:write")
    out = await dep(principal=principal)  # type: ignore[arg-type]
    assert out is principal


@pytest.mark.asyncio
async def test_require_scope_wildcard_allows_everything():
    """A key with ``*`` passes every scope check."""
    tenant = SimpleNamespace(id=uuid.uuid4())
    principal = AuthPrincipal(
        tenant=tenant, user=None, role="admin", source="api_key", scopes=["*"]
    )
    for scope in ("interactions:write", "kb:write", "users:write", "gdpr:delete"):
        dep = require_scope(scope)
        assert await dep(principal=principal) is principal  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_require_scope_session_principal_bypass():
    """Session-JWT principals are not gated by scopes — only by roles."""
    tenant = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    principal = AuthPrincipal(
        tenant=tenant, user=user, role="admin", source="session", scopes=[]
    )
    dep = require_scope("interactions:write")
    assert await dep(principal=principal) is principal  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_require_scope_clerk_principal_bypass():
    tenant = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    principal = AuthPrincipal(
        tenant=tenant, user=user, role="admin", source="clerk", scopes=[]
    )
    dep = require_scope("kb:write")
    assert await dep(principal=principal) is principal  # type: ignore[arg-type]


def test_require_scope_unknown_scope_fails_at_definition():
    """Typos in source code fail loudly at import / definition time."""
    with pytest.raises(ValueError):
        require_scope("not_a_real_scope:x")


# ── _principal_from_api_key attaches scopes ──────────────────────────


@pytest.mark.asyncio
async def test_principal_from_api_key_attaches_scopes():
    api_key = "csk_" + "B" * 48
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {api_key}".encode())],
    }
    request = Request(scope)

    tenant_obj = SimpleNamespace(id=uuid.uuid4())
    api_key_row = SimpleNamespace(
        tenant=tenant_obj,
        expires_at=None,
        last_used_at=None,
        scopes=["interactions:write", "webhooks:read"],
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return api_key_row

    class FakeDB:
        async def execute(self, _stmt):
            return FakeResult()

    principal = await auth_mod._principal_from_api_key(request, FakeDB())
    assert principal is not None
    assert principal.source == "api_key"
    assert principal.scopes == ["interactions:write", "webhooks:read"]
    assert principal.has_scope("interactions:write") is True
    assert principal.has_scope("kb:write") is False


@pytest.mark.asyncio
async def test_principal_from_api_key_handles_null_scopes_legacy_row():
    """Pre-migration rows may carry NULL scopes — coerce to empty list."""
    api_key = "csk_" + "C" * 48
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {api_key}".encode())],
    }
    request = Request(scope)

    tenant_obj = SimpleNamespace(id=uuid.uuid4())
    api_key_row = SimpleNamespace(
        tenant=tenant_obj,
        expires_at=None,
        last_used_at=None,
        scopes=None,
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return api_key_row

    class FakeDB:
        async def execute(self, _stmt):
            return FakeResult()

    principal = await auth_mod._principal_from_api_key(request, FakeDB())
    assert principal is not None
    assert principal.scopes == []
    assert principal.has_scope("kb:write") is False


# ── Pydantic schema validation on create ─────────────────────────────


def test_create_request_rejects_unknown_scope():
    from pydantic import ValidationError
    from backend.app.api.api_keys import ApiKeyCreateRequest

    with pytest.raises(ValidationError):
        ApiKeyCreateRequest(scopes=["bogus:scope"])


def test_create_request_collapses_wildcard():
    from backend.app.api.api_keys import ApiKeyCreateRequest

    req = ApiKeyCreateRequest(scopes=["*", "kb:read"])
    assert req.scopes == ["*"]


def test_create_request_default_is_none():
    """Empty / omitted scopes → None (the endpoint substitutes the default)."""
    from backend.app.api.api_keys import ApiKeyCreateRequest

    assert ApiKeyCreateRequest().scopes is None


# ── Persistence test (in-memory SQLite) ──────────────────────────────


@pytest.mark.asyncio
async def test_api_key_scopes_persist_round_trip(test_session_factory, test_tenant):
    """Create → read back. The DB column round-trips arbitrary JSON."""
    from backend.app.models import ApiKey

    async with test_session_factory() as session:
        key = ApiKey(
            tenant_id=test_tenant.id,
            key_hash="abc" * 20,
            name="round trip",
            scopes=["interactions:write", "kb:read"],
        )
        session.add(key)
        await session.commit()
        await session.refresh(key)
        assert key.scopes == ["interactions:write", "kb:read"]

        # Mutate + reload — simulating PATCH /api-keys/{id}.
        key.scopes = ["*"]
        await session.commit()
        await session.refresh(key)
        assert key.scopes == ["*"]


def test_canonical_scope_set_includes_required_namespaces():
    """Every documented scope is in the canonical set."""
    required = {
        "interactions:read",
        "interactions:write",
        "action_items:write",
        "analytics:read",
        "webhooks:write",
        "kb:read",
        "kb:write",
        "crm:sync",
        "gdpr:export",
        "gdpr:delete",
        "users:write",
        "api_keys:write",
        "*",
    }
    missing = required - set(API_KEY_SCOPES)
    assert not missing, f"missing canonical scopes: {missing}"
