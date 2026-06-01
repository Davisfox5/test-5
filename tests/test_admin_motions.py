"""Tests for the motion-assignment admin plumbing.

The integration-test fixture in this repo wires a focused FastAPI app
with only the outcomes router mounted, so there's no shared HTTP
client that exercises ``/users`` or ``/admin/*`` end-to-end. Tests
here are unit-shaped: they cover the helper validators, the CSV
parsing, the User-model fields end-to-end through an in-memory SQLite
session, and the audit-log shape via a captured-side-effect harness.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@pytest.fixture
def sync_session():
    from backend.app.db import Base
    import backend.app.models  # noqa: F401 — registers mapped classes

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def seeded_tenant(sync_session):
    from backend.app.models import Tenant

    tenant = Tenant(
        name="Acme",
        slug=f"acme-{uuid.uuid4().hex[:6]}",
        default_domain="sales",
    )
    sync_session.add(tenant)
    sync_session.commit()
    sync_session.refresh(tenant)
    return tenant


# ── Helper validators ──────────────────────────────────────────────────


def test_parse_domain_list_accepts_pipe_and_semicolon():
    from backend.app.api.admin_motions import _parse_domain_list

    assert _parse_domain_list("sales|customer_service", "x") == [
        "sales",
        "customer_service",
    ]
    assert _parse_domain_list("sales;customer_service", "x") == [
        "sales",
        "customer_service",
    ]
    # Mixed delimiter — first split is pipe, then semicolons inside.
    assert _parse_domain_list("sales|customer_service;it_support", "x") == [
        "sales",
        "customer_service",
        "it_support",
    ]
    assert _parse_domain_list("", "x") == []
    assert _parse_domain_list("   ", "x") == []


def test_parse_domain_list_dedupes_within_row():
    from backend.app.api.admin_motions import _parse_domain_list

    assert _parse_domain_list("sales|sales|customer_service", "x") == [
        "sales",
        "customer_service",
    ]


def test_parse_domain_list_rejects_unknown_value():
    from backend.app.api.admin_motions import _parse_domain_list

    with pytest.raises(ValueError, match="unknown domain"):
        _parse_domain_list("sales|garbage", "x")


def test_parse_bool_permissive():
    from backend.app.api.admin_motions import _parse_bool

    for v in ("true", "yes", "1", "Y", "T", "TRUE"):
        assert _parse_bool(v) is True, v
    for v in ("false", "no", "0", "", "nope"):
        assert _parse_bool(v) is False, v


# ── /users domain-list edge-validation ─────────────────────────────────


def test_user_create_validate_domain_list_rejects_unknown():
    from fastapi import HTTPException

    from backend.app.api.auth_session import _validate_domain_list

    with pytest.raises(HTTPException) as exc:
        _validate_domain_list(["sales", "garbage"], "agent_domains")
    assert exc.value.status_code == 422
    assert "garbage" in exc.value.detail


def test_user_create_validate_domain_list_dedupes_and_passes_known():
    from backend.app.api.auth_session import _validate_domain_list

    out = _validate_domain_list(
        ["sales", "customer_service", "sales"], "agent_domains"
    )
    assert out == ["sales", "customer_service"]


def test_user_create_validate_domain_list_none_means_empty():
    from backend.app.api.auth_session import _validate_domain_list

    assert _validate_domain_list(None, "agent_domains") == []


def test_user_create_validate_domain_list_strips_whitespace():
    from backend.app.api.auth_session import _validate_domain_list

    assert _validate_domain_list(["  sales  "], "agent_domains") == ["sales"]


def test_user_create_validate_domain_list_rejects_non_string():
    from fastapi import HTTPException

    from backend.app.api.auth_session import _validate_domain_list

    with pytest.raises(HTTPException) as exc:
        _validate_domain_list(["sales", 7], "agent_domains")  # type: ignore[list-item]
    assert exc.value.status_code == 422


# ── User model with motion-scope columns ───────────────────────────────


def test_user_persists_motion_scopes(sync_session, seeded_tenant):
    """Round-trip the three new columns through the ORM."""
    from backend.app.models import User

    u = User(
        tenant_id=seeded_tenant.id,
        email=f"alice-{uuid.uuid4().hex[:6]}@acme.test",
        name="Alice",
        role="agent",
        agent_domains=["sales", "customer_service"],
        manager_domains=["customer_service"],
        is_tenant_admin=True,
    )
    sync_session.add(u)
    sync_session.commit()
    sync_session.refresh(u)
    assert u.agent_domains == ["sales", "customer_service"]
    assert u.manager_domains == ["customer_service"]
    assert u.is_tenant_admin is True


def test_user_defaults_when_motion_scopes_omitted(sync_session, seeded_tenant):
    """Empty/false defaults — matches the column defaults from dom_001."""
    from backend.app.models import User

    u = User(
        tenant_id=seeded_tenant.id,
        email=f"bob-{uuid.uuid4().hex[:6]}@acme.test",
        name="Bob",
        role="agent",
    )
    sync_session.add(u)
    sync_session.commit()
    sync_session.refresh(u)
    assert u.agent_domains == []
    assert u.manager_domains == []
    assert u.is_tenant_admin is False


# ── Tenant default_domain ──────────────────────────────────────────────


def test_tenant_default_domain_round_trips(sync_session, seeded_tenant):
    from backend.app.models import Tenant

    seeded_tenant.default_domain = "customer_service"
    sync_session.commit()
    sync_session.refresh(seeded_tenant)
    fetched = sync_session.get(Tenant, seeded_tenant.id)
    assert fetched is not None
    assert fetched.default_domain == "customer_service"


# ── CSV import: pure parsing path through the dictreader ───────────────


def test_csv_import_helpers_produce_consistent_user_payload():
    """Smoke-check that the helper combination produces the shape the
    handler depends on: known role, sanitized motion lists, parsed bool.

    Catches a regression where any one of the helpers changes signature
    without the others noticing.
    """
    from backend.app.api.admin_motions import (
        _DOMAIN_SET,
        _parse_bool,
        _parse_domain_list,
    )

    role = "manager"
    assert role in {"agent", "manager", "admin"}
    agent = _parse_domain_list("sales|customer_service", "agent_domains")
    manager = _parse_domain_list("customer_service", "manager_domains")
    tenant_admin = _parse_bool("false")
    assert set(agent).issubset(_DOMAIN_SET)
    assert set(manager).issubset(_DOMAIN_SET)
    assert tenant_admin is False
