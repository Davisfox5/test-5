"""Tests for SSO/SCIM motion-scope provisioning.

Covers:
* ``resolve_scopes_from_groups`` — empty input, no matches, single
  match, multi-rule union, inactive rules ignored, tenant isolation.
* ``apply_scopes_to_user`` — patch semantics + return value when
  nothing changes + ``overwrite_admin`` toggle.

The HTTP endpoints (``/admin/motion-provisioning-rules``,
``/scim/v2/Users``) are thin wrappers around these helpers; the
project's HTTP fixture only mounts the outcomes router, so the
admin/scim endpoints would need their own test harness — deferred to
a follow-up.
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
    import backend.app.models  # noqa: F401

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def seeded(sync_session):
    from backend.app.models import Tenant, User

    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
    sync_session.add(tenant)
    sync_session.commit()
    user = User(
        tenant_id=tenant.id,
        email=f"u-{uuid.uuid4().hex[:6]}@acme.test",
        role="agent",
    )
    sync_session.add(user)
    sync_session.commit()
    sync_session.refresh(tenant)
    sync_session.refresh(user)
    return tenant, user


def _add_rule(
    sync_session,
    tenant_id,
    *,
    group_name: str,
    agent_domains=None,
    manager_domains=None,
    grants_admin: bool = False,
    is_active: bool = True,
):
    from backend.app.models import MotionProvisioningRule

    r = MotionProvisioningRule(
        tenant_id=tenant_id,
        group_name=group_name,
        agent_domains=agent_domains or [],
        manager_domains=manager_domains or [],
        grants_tenant_admin=grants_admin,
        is_active=is_active,
    )
    sync_session.add(r)
    sync_session.commit()
    return r


# ── resolve_scopes_from_groups ─────────────────────────────────────────


def test_resolve_returns_empty_when_no_groups(sync_session, seeded):
    from backend.app.services.sso_provisioning import resolve_scopes_from_groups

    tenant, _user = seeded
    out = resolve_scopes_from_groups(sync_session, tenant.id, [])
    assert out.matched_rule_count == 0
    assert out.agent_domains == []
    assert out.manager_domains == []
    assert out.is_tenant_admin is False


def test_resolve_skips_unknown_groups(sync_session, seeded):
    """A group with no matching rule isn't an error — just a silent
    skip. Lets an IDP push a noisy claim set without forcing the
    tenant to map every value."""
    from backend.app.services.sso_provisioning import resolve_scopes_from_groups

    tenant, _user = seeded
    _add_rule(sync_session, tenant.id, group_name="linda-sales", agent_domains=["sales"])
    out = resolve_scopes_from_groups(
        sync_session, tenant.id, ["totally-unknown-group"]
    )
    assert out.matched_rule_count == 0
    assert out.agent_domains == []


def test_resolve_single_rule_match(sync_session, seeded):
    from backend.app.services.sso_provisioning import resolve_scopes_from_groups

    tenant, _user = seeded
    _add_rule(
        sync_session,
        tenant.id,
        group_name="linda-cs-managers",
        manager_domains=["customer_service"],
    )
    out = resolve_scopes_from_groups(
        sync_session, tenant.id, ["linda-cs-managers"]
    )
    assert out.matched_rule_count == 1
    assert out.manager_domains == ["customer_service"]
    assert out.agent_domains == []


def test_resolve_unions_across_rules(sync_session, seeded):
    """Two rules, two groups: the scope union is the result, with no
    duplicates inside a single list (set-shaped membership)."""
    from backend.app.services.sso_provisioning import resolve_scopes_from_groups

    tenant, _user = seeded
    _add_rule(
        sync_session,
        tenant.id,
        group_name="linda-sales-agents",
        agent_domains=["sales"],
    )
    _add_rule(
        sync_session,
        tenant.id,
        group_name="linda-cs-managers",
        agent_domains=["customer_service"],
        manager_domains=["customer_service"],
    )
    out = resolve_scopes_from_groups(
        sync_session, tenant.id, ["linda-sales-agents", "linda-cs-managers"]
    )
    assert out.matched_rule_count == 2
    assert sorted(out.agent_domains) == ["customer_service", "sales"]
    assert out.manager_domains == ["customer_service"]


def test_resolve_inactive_rules_ignored(sync_session, seeded):
    from backend.app.services.sso_provisioning import resolve_scopes_from_groups

    tenant, _user = seeded
    _add_rule(
        sync_session,
        tenant.id,
        group_name="linda-sales-agents",
        agent_domains=["sales"],
        is_active=False,
    )
    out = resolve_scopes_from_groups(
        sync_session, tenant.id, ["linda-sales-agents"]
    )
    assert out.matched_rule_count == 0
    assert out.agent_domains == []


def test_resolve_tenant_isolation(sync_session):
    """A rule on tenant A must not match for a user signing into tenant B."""
    from backend.app.models import Tenant
    from backend.app.services.sso_provisioning import resolve_scopes_from_groups

    a = Tenant(name="A", slug=f"a-{uuid.uuid4().hex[:6]}")
    b = Tenant(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    sync_session.add_all([a, b])
    sync_session.commit()
    _add_rule(
        sync_session,
        a.id,
        group_name="linda-sales-agents",
        agent_domains=["sales"],
    )
    out_b = resolve_scopes_from_groups(
        sync_session, b.id, ["linda-sales-agents"]
    )
    assert out_b.matched_rule_count == 0


def test_resolve_tenant_admin_grant_aggregates(sync_session, seeded):
    """Any single matching rule with grants_tenant_admin=True should
    flip the bit. Multiple admin-granting rules don't elevate beyond
    True."""
    from backend.app.services.sso_provisioning import resolve_scopes_from_groups

    tenant, _user = seeded
    _add_rule(
        sync_session,
        tenant.id,
        group_name="linda-admins",
        grants_admin=True,
    )
    _add_rule(
        sync_session,
        tenant.id,
        group_name="linda-cs",
        manager_domains=["customer_service"],
    )
    out = resolve_scopes_from_groups(
        sync_session, tenant.id, ["linda-admins", "linda-cs"]
    )
    assert out.is_tenant_admin is True


# ── apply_scopes_to_user ───────────────────────────────────────────────


def test_apply_writes_when_diff(sync_session, seeded):
    from backend.app.services.sso_provisioning import (
        ResolvedScopes,
        apply_scopes_to_user,
    )

    _tenant, user = seeded
    changed = apply_scopes_to_user(
        sync_session,
        user,
        ResolvedScopes(["sales"], ["sales"], True, 2),
    )
    assert changed is True
    assert user.agent_domains == ["sales"]
    assert user.manager_domains == ["sales"]
    assert user.is_tenant_admin is True


def test_apply_returns_false_when_noop(sync_session, seeded):
    from backend.app.services.sso_provisioning import (
        ResolvedScopes,
        apply_scopes_to_user,
    )

    _tenant, user = seeded
    user.agent_domains = ["sales"]
    user.manager_domains = []
    user.is_tenant_admin = False
    sync_session.commit()
    changed = apply_scopes_to_user(
        sync_session,
        user,
        ResolvedScopes(["sales"], [], False, 1),
    )
    assert changed is False


def test_apply_overwrite_admin_false_keeps_admin_bit(sync_session, seeded):
    """When ``overwrite_admin=False`` we don't drop a manually-granted
    tenant-admin bit just because the SCIM payload's group set didn't
    include the admin-granting group."""
    from backend.app.services.sso_provisioning import (
        ResolvedScopes,
        apply_scopes_to_user,
    )

    _tenant, user = seeded
    user.is_tenant_admin = True
    sync_session.commit()
    apply_scopes_to_user(
        sync_session,
        user,
        ResolvedScopes(["sales"], [], False, 1),
        overwrite_admin=False,
    )
    assert user.is_tenant_admin is True
