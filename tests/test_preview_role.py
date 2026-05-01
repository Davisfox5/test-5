"""Tests for the sandbox-only role-preview switcher.

Covers the three-layer security gate (tier + trial-active + role
validity), the ``POST /me/preview-role`` endpoint, the principal-
resolver overlay, the Stripe-webhook tier-transition clearing, and the
DB-level CHECK constraint that pins the column vocabulary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.app.api.me import router as me_router
from backend.app.api.stripe_webhook import _apply_subscription_to_tenant
from backend.app.auth import (
    AuthPrincipal,
    _resolve_effective_role,
    get_current_principal,
)
from backend.app.db import get_db
from backend.app.models import AuditLog, Tenant, User


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sandbox_tenant_and_user(test_session_factory):
    """Seed a sandbox tenant with an active trial + an admin user.

    Trial ends 14 days out, so all three gate layers (tier + trial
    active + role validity) pass by default.
    """
    async with test_session_factory() as session:
        tenant = Tenant(
            name="Sandbox Co",
            slug=f"sb-{uuid.uuid4().hex[:8]}",
            plan_tier="sandbox",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

        user = User(
            tenant_id=tenant.id,
            email="admin@sandbox.example",
            name="Sandbox Admin",
            role="admin",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return tenant, user


@pytest_asyncio.fixture
async def me_app(test_session_factory, sandbox_tenant_and_user) -> AsyncIterator[FastAPI]:
    """FastAPI app hosting just the /me router with a fixed principal.

    The principal is rebuilt on every request from the latest tenant +
    user rows so the resolver's overlay logic (which reads
    ``user.preview_role`` and ``tenant.trial_ends_at``) sees committed
    state rather than a stale snapshot.
    """
    tenant, user = sandbox_tenant_and_user

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_get_principal():
        async with test_session_factory() as session:
            db_user = (
                await session.execute(select(User).where(User.id == user.id))
            ).scalar_one()
            db_tenant = (
                await session.execute(select(Tenant).where(Tenant.id == tenant.id))
            ).scalar_one()
            # SQLite strips tzinfo on roundtrip; production (Postgres)
            # preserves it. Re-apply UTC so the principal resolver and
            # the trial helpers compare aware-vs-aware.
            if (
                db_tenant.trial_ends_at is not None
                and db_tenant.trial_ends_at.tzinfo is None
            ):
                db_tenant.trial_ends_at = db_tenant.trial_ends_at.replace(
                    tzinfo=timezone.utc
                )
            effective_role, is_previewing = _resolve_effective_role(db_user, db_tenant)
            return AuthPrincipal(
                tenant=db_tenant,
                user=db_user,
                role=effective_role,
                source="session",
                is_previewing=is_previewing,
            )

    app = FastAPI()
    app.include_router(me_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_principal] = _override_get_principal
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def me_client(me_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=me_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── Happy path: set / clear ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_sets_preview_role_and_me_reflects_overlay(
    me_client, test_session_factory, sandbox_tenant_and_user
):
    _, user = sandbox_tenant_and_user
    resp = await me_client.post("/api/v1/me/preview-role", json={"role": "manager"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "manager"
    assert body["real_role"] == "admin"

    # The DB row was updated.
    async with test_session_factory() as session:
        db_user = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert db_user.preview_role == "manager"
        # Real role untouched — preview is render-time only.
        assert db_user.role == "admin"

    # /me reflects the overlay: effective role = manager, is_previewing
    # = true, real_role still admin.
    me_resp = await me_client.get("/api/v1/me")
    assert me_resp.status_code == 200
    me_body = me_resp.json()
    assert me_body["user"]["role"] == "manager"
    assert me_body["user"]["real_role"] == "admin"
    assert me_body["user"]["is_previewing"] is True
    assert me_body["user"]["preview_role"] == "manager"


@pytest.mark.asyncio
async def test_clearing_preview_role_reverts(
    me_client, test_session_factory, sandbox_tenant_and_user
):
    _, user = sandbox_tenant_and_user
    # Set first.
    set_resp = await me_client.post(
        "/api/v1/me/preview-role", json={"role": "agent"}
    )
    assert set_resp.status_code == 200

    # Clear with role: null.
    clear_resp = await me_client.post(
        "/api/v1/me/preview-role", json={"role": None}
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["role"] is None

    async with test_session_factory() as session:
        db_user = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert db_user.preview_role is None

    me_body = (await me_client.get("/api/v1/me")).json()
    assert me_body["user"]["role"] == "admin"
    assert me_body["user"]["is_previewing"] is False
    assert me_body["user"]["preview_role"] is None


@pytest.mark.asyncio
async def test_post_writes_audit_log_row(
    me_client, test_session_factory, sandbox_tenant_and_user
):
    _, user = sandbox_tenant_and_user
    resp = await me_client.post("/api/v1/me/preview-role", json={"role": "manager"})
    assert resp.status_code == 200

    async with test_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "user.preview_role_set")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].resource_id == str(user.id)
        assert rows[0].after == {"preview_role": "manager"}


# ── 403s: tenant tier / trial / API-key ──────────────────────────────


@pytest.mark.asyncio
async def test_403_when_tenant_not_sandbox(
    me_client, test_session_factory, sandbox_tenant_and_user
):
    """Tenants on starter/growth/enterprise can't write a preview role."""
    tenant, _ = sandbox_tenant_and_user
    async with test_session_factory() as session:
        db_tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        ).scalar_one()
        db_tenant.plan_tier = "starter"
        await session.commit()

    resp = await me_client.post("/api/v1/me/preview-role", json={"role": "manager"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "preview role is sandbox-only"


@pytest.mark.asyncio
async def test_403_when_trial_expired(
    me_client, test_session_factory, sandbox_tenant_and_user
):
    """Sandbox tenants past their trial cutoff get the trial-expired 403."""
    tenant, _ = sandbox_tenant_and_user
    async with test_session_factory() as session:
        db_tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        ).scalar_one()
        db_tenant.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
        await session.commit()

    resp = await me_client.post("/api/v1/me/preview-role", json={"role": "manager"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "trial expired; preview role is unavailable"


@pytest.mark.asyncio
async def test_403_for_api_key_principal(test_session_factory, sandbox_tenant_and_user):
    """API-key callers have no human user behind them — preview is 403."""
    tenant, _ = sandbox_tenant_and_user

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_get_principal():
        async with test_session_factory() as session:
            db_tenant = (
                await session.execute(select(Tenant).where(Tenant.id == tenant.id))
            ).scalar_one()
            if (
                db_tenant.trial_ends_at is not None
                and db_tenant.trial_ends_at.tzinfo is None
            ):
                db_tenant.trial_ends_at = db_tenant.trial_ends_at.replace(
                    tzinfo=timezone.utc
                )
            return AuthPrincipal(
                tenant=db_tenant,
                user=None,
                role="admin",
                source="api_key",
                scopes=["*"],
            )

    app = FastAPI()
    app.include_router(me_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_principal] = _override_get_principal

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/me/preview-role", json={"role": "manager"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "preview role only applies to interactive sessions"


@pytest.mark.asyncio
async def test_422_for_invalid_role(me_client):
    """Roles outside the literal set fail Pydantic validation with 422."""
    resp = await me_client.post("/api/v1/me/preview-role", json={"role": "owner"})
    assert resp.status_code == 422


# ── Resolver gate: overlay applied only when all three layers pass ───


@pytest.mark.asyncio
async def test_resolver_does_not_apply_overlay_for_non_sandbox_tenant(
    test_session_factory, sandbox_tenant_and_user
):
    tenant, user = sandbox_tenant_and_user
    user.preview_role = "manager"
    tenant.plan_tier = "starter"
    role, is_previewing = _resolve_effective_role(user, tenant)
    assert role == "admin"
    assert is_previewing is False


@pytest.mark.asyncio
async def test_resolver_does_not_apply_overlay_when_trial_expired(
    sandbox_tenant_and_user,
):
    tenant, user = sandbox_tenant_and_user
    user.preview_role = "manager"
    tenant.trial_ends_at = datetime.now(timezone.utc) - timedelta(hours=1)
    role, is_previewing = _resolve_effective_role(user, tenant)
    assert role == "admin"
    assert is_previewing is False


@pytest.mark.asyncio
async def test_resolver_falls_through_when_preview_role_invalid(
    sandbox_tenant_and_user,
):
    tenant, user = sandbox_tenant_and_user
    # In normal operation the CHECK constraint blocks this — but the
    # resolver is also defensive against legacy / corrupted rows.
    user.preview_role = "owner"
    role, is_previewing = _resolve_effective_role(user, tenant)
    assert role == "admin"
    assert is_previewing is False


# ── Stripe webhook clears preview_role on tier transition ───────────


@pytest.mark.asyncio
async def test_tier_change_to_starter_clears_all_preview_roles(
    test_session_factory, sandbox_tenant_and_user, monkeypatch
):
    """A subscription event lifting the tenant off sandbox clears every
    user's preview_role in that tenant."""
    from backend.app.api import stripe_webhook as sw

    tenant, _ = sandbox_tenant_and_user

    # Seed two more users, all with active preview_role overrides, so we
    # can prove the clearing affects every user in the tenant.
    async with test_session_factory() as session:
        u1 = User(
            tenant_id=tenant.id,
            email="agent@sandbox.example",
            role="agent",
            preview_role="manager",
        )
        u2 = User(
            tenant_id=tenant.id,
            email="manager@sandbox.example",
            role="manager",
            preview_role="admin",
        )
        first = (
            await session.execute(select(User).where(User.tenant_id == tenant.id))
        ).scalars().all()
        assert len(first) == 1
        first[0].preview_role = "agent"
        session.add_all([u1, u2])
        await session.commit()

    # Stub Stripe price → tier mapping, customer linkage, and seat
    # reconciliation so the webhook handler runs without external deps.
    monkeypatch.setattr(sw, "price_id_to_tier", lambda price_id: "starter")

    async def _fake_tenant_by_customer(_db, _customer_id):
        return tenant

    monkeypatch.setattr(sw, "_tenant_by_customer", _fake_tenant_by_customer)

    async def _fake_reconcile(_db, t):
        from backend.app.services.seat_reconciliation import ReconcileResult

        return ReconcileResult(
            tenant_id=t.id,
            suspended_user_ids=[],
            suspended_admin_ids=[],
            pending=False,
        )

    monkeypatch.setattr(sw, "reconcile_seats", _fake_reconcile)

    fake_subscription = {
        "id": "sub_test_123",
        "customer": "cus_test_123",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_starter_monthly"}}]},
    }

    async with test_session_factory() as session:
        # Re-attach tenant to this session so apply_tier writes propagate.
        db_tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        ).scalar_one()

        async def _fake_tenant_by_customer_session(_db, _customer_id):
            return db_tenant

        monkeypatch.setattr(sw, "_tenant_by_customer", _fake_tenant_by_customer_session)

        result = await _apply_subscription_to_tenant(session, fake_subscription)
        await session.commit()
        assert result["handled"] is True
        assert result["tier"] == "starter"

    # Every preview_role for users in this tenant is now NULL.
    async with test_session_factory() as session:
        users = (
            await session.execute(select(User).where(User.tenant_id == tenant.id))
        ).scalars().all()
        assert len(users) == 3
        assert all(u.preview_role is None for u in users)


# ── DB CHECK constraint ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_check_constraint_rejects_invalid_preview_role(
    test_session_factory, sandbox_tenant_and_user
):
    """The CHECK constraint pins the column to {agent, manager, admin}
    or NULL. ``'owner'`` (or any other string) must be rejected at the
    DB level — defense-in-depth alongside Pydantic.

    SQLite's ``CREATE TABLE`` from SQLAlchemy emits CHECK constraints
    natively, so an in-memory DB enforces them just like Postgres.
    """
    tenant, _ = sandbox_tenant_and_user
    async with test_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role, preview_role, "
                    "is_active) VALUES (:id, :tid, :email, :role, :pr, 1)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": str(tenant.id),
                    "email": "rogue@sandbox.example",
                    "role": "admin",
                    "pr": "owner",
                },
            )
            await session.commit()
