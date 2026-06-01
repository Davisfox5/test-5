"""Tests for the ``resource_id`` filter on ``/admin/audit-logs``.

Added with the admin user-profile drawer: the drawer needs to fetch
just the audit entries for one user, which is a tenant-scoped
``resource_type=user`` + ``resource_id=<uuid>`` query.

Tests stay at the pure-data-access layer (in-memory SQLite) because
the existing audit-log unit tests do the same and the project doesn't
have an HTTP fixture for ``/admin/*``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
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


def _add_audit_row(
    sync_session,
    tenant_id,
    *,
    resource_id: str,
    action: str = "user.updated",
    resource_type: str = "user",
):
    from backend.app.models import AuditLog

    row = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=None,
        actor_principal="system",
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before={},
        after={},
        meta={},  # python attr; SQL column is named ``metadata``
    )
    sync_session.add(row)
    sync_session.flush()
    return row


def test_filter_by_resource_id_returns_only_matching_rows(sync_session):
    """The new filter on the admin audit-log endpoint: scoping to a
    single user's rows should drop every other resource_id."""
    from backend.app.models import AuditLog

    tenant_id = uuid.uuid4()
    target_user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()

    _add_audit_row(sync_session, tenant_id, resource_id=str(target_user_id))
    _add_audit_row(sync_session, tenant_id, resource_id=str(other_user_id))
    _add_audit_row(
        sync_session,
        tenant_id,
        resource_id=str(target_user_id),
        action="user.imported",
    )
    sync_session.commit()

    rows = (
        sync_session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.resource_type == "user",
                AuditLog.resource_id == str(target_user_id),
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    actions = {r.action for r in rows}
    assert actions == {"user.updated", "user.imported"}


def test_filter_resource_id_does_not_leak_across_tenants(sync_session):
    """Even with the same resource_id literal, audit entries from
    another tenant must not appear — the endpoint's tenant filter is
    the load-bearing security boundary."""
    from backend.app.models import AuditLog

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    rid = str(uuid.uuid4())

    _add_audit_row(sync_session, tenant_a, resource_id=rid)
    _add_audit_row(sync_session, tenant_b, resource_id=rid)
    sync_session.commit()

    rows = (
        sync_session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a,
                AuditLog.resource_id == rid,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant_a


def test_resource_id_filter_accepts_synthetic_ids(sync_session):
    """``audit_log.resource_id`` is a string column so colon-namespaced
    synthetic ids (``tenant:settings:features``) need to filter cleanly
    alongside UUID resource ids."""
    from backend.app.models import AuditLog

    tenant_id = uuid.uuid4()
    synthetic = "tenant:settings:features"
    _add_audit_row(
        sync_session,
        tenant_id,
        resource_id=synthetic,
        action="tenant.settings.updated",
        resource_type="tenant",
    )
    sync_session.commit()

    rows = (
        sync_session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.resource_id == synthetic,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].resource_id == synthetic
