"""Tests for Clerk SSO just-in-time provisioning.

Covers the three behaviours that make Clerk-brokered enterprise SSO
actually land a user: link-by-email, create-when-permitted, and the
fail-closed rejections (disabled, unmapped tenant, ambiguous match).
Runs on the SQLite DB fixtures; ``bind_tenant_async`` is a no-op there.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.config import get_settings
from backend.app.models import Tenant, User
from backend.app.services.sso_jit import resolve_or_provision_clerk_user


@pytest.fixture
def jit_enabled(monkeypatch):
    # get_settings() is cached; flip the flag on the live instance.
    monkeypatch.setattr(get_settings(), "SSO_JIT_PROVISIONING_ENABLED", True)


async def _mk_tenant(db, sso: dict) -> Tenant:
    tenant = Tenant(
        name="Acme",
        slug=f"acme-{uuid.uuid4().hex[:8]}",
        features_enabled={"sso": sso},
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@pytest.mark.asyncio
async def test_links_existing_user_by_email(test_session_factory, jit_enabled):
    async with test_session_factory() as db:
        tenant = await _mk_tenant(db, {"clerk_org_ids": ["org_x"]})
        user = User(
            tenant_id=tenant.id,
            email="alice@acme.com",
            clerk_user_id=None,
            role="agent",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        user_id = user.id

        ok = await resolve_or_provision_clerk_user(
            db, {"org_id": "org_x", "email": "alice@acme.com"}, "user_123"
        )
        assert ok is True

        linked = await db.get(User, user_id)
        assert linked.clerk_user_id == "user_123"


@pytest.mark.asyncio
async def test_creates_user_when_jit_create_enabled(
    test_session_factory, jit_enabled
):
    async with test_session_factory() as db:
        tenant = await _mk_tenant(
            db,
            {
                "email_domains": ["acme.com"],
                "jit_create": True,
                "default_role": "manager",
            },
        )
        ok = await resolve_or_provision_clerk_user(
            db, {"email": "new@acme.com", "name": "New Person"}, "user_999"
        )
        assert ok is True

        from sqlalchemy import select

        created = (
            await db.execute(
                select(User).where(User.clerk_user_id == "user_999")
            )
        ).scalar_one()
        assert created.email == "new@acme.com"
        assert created.tenant_id == tenant.id
        assert created.role == "manager"
        assert created.name == "New Person"


@pytest.mark.asyncio
async def test_rejects_when_no_jit_create_and_no_invited_user(
    test_session_factory, jit_enabled
):
    async with test_session_factory() as db:
        await _mk_tenant(db, {"email_domains": ["acme.com"]})  # no jit_create
        ok = await resolve_or_provision_clerk_user(
            db, {"email": "ghost@acme.com"}, "user_404"
        )
        assert ok is False


@pytest.mark.asyncio
async def test_rejects_unmapped_token(test_session_factory, jit_enabled):
    async with test_session_factory() as db:
        await _mk_tenant(db, {"clerk_org_ids": ["org_x"], "jit_create": True})
        ok = await resolve_or_provision_clerk_user(
            db, {"org_id": "org_other", "email": "x@nowhere.com"}, "user_1"
        )
        assert ok is False


@pytest.mark.asyncio
async def test_disabled_by_default(test_session_factory):
    # No jit_enabled fixture → flag stays False.
    async with test_session_factory() as db:
        await _mk_tenant(db, {"clerk_org_ids": ["org_x"], "jit_create": True})
        ok = await resolve_or_provision_clerk_user(
            db, {"org_id": "org_x", "email": "a@acme.com"}, "user_1"
        )
        assert ok is False


@pytest.mark.asyncio
async def test_never_hijacks_email_bound_to_other_clerk_id(
    test_session_factory, jit_enabled
):
    async with test_session_factory() as db:
        tenant = await _mk_tenant(db, {"clerk_org_ids": ["org_x"]})
        db.add(
            User(
                tenant_id=tenant.id,
                email="taken@acme.com",
                clerk_user_id="user_original",
                role="agent",
                is_active=True,
            )
        )
        await db.commit()

        ok = await resolve_or_provision_clerk_user(
            db, {"org_id": "org_x", "email": "taken@acme.com"}, "user_impostor"
        )
        assert ok is False
