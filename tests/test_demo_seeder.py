"""Tests for the demo / sandbox sample-data seeder.

Covers:
- Per-resource created counts on a fresh tenant.
- Idempotency: a second pass adds zero rows (interactions already there
  short-circuits that section; scorecard / kb / webhook checks dedupe
  by name / url).
- Top-up: a tenant with interactions but no scorecards / kb still gets
  scorecards / kb / webhook on a re-run (real-world recovery path).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.app.models import (
    ActionItem,
    Interaction,
    InteractionFeatures,
    KBDocument,
    ScorecardTemplate,
    Tenant,
    User,
    Webhook,
)
from backend.app.services.demo_seeder import seed_demo_data


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def seeded_tenant(test_session_factory):
    """Create a fresh tenant + admin user for the seeder to fill."""
    async with test_session_factory() as session:
        tenant = Tenant(name="Demo Co", slug=f"demo-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        admin = User(
            tenant_id=tenant.id,
            email=f"admin-{uuid.uuid4().hex[:6]}@demo.example",
            role="admin",
        )
        session.add(admin)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(admin)
        return tenant, admin


async def _count(session, model, tenant_id) -> int:
    stmt = select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    return (await session.execute(stmt)).scalar_one()


async def test_seeder_creates_expected_counts(test_session_factory, seeded_tenant):
    tenant, admin = seeded_tenant
    async with test_session_factory() as session:
        tenant_in = await session.get(Tenant, tenant.id)
        admin_in = await session.get(User, admin.id)
        counts = await seed_demo_data(
            session, tenant=tenant_in, admin_user=admin_in
        )

    assert counts == {
        "scorecards": 2,
        "interactions": 8,
        "action_items": 6,
        "kb_docs": 2,
        "webhooks": 1,
    }

    async with test_session_factory() as session:
        assert await _count(session, ScorecardTemplate, tenant.id) == 2
        assert await _count(session, Interaction, tenant.id) == 8
        assert await _count(session, ActionItem, tenant.id) == 6
        assert await _count(session, KBDocument, tenant.id) == 2
        assert await _count(session, Webhook, tenant.id) == 1
        # InteractionFeatures rows shadow each interaction so the
        # orchestrator + scorer don't 404 looking for them.
        feat_stmt = (
            select(func.count())
            .select_from(InteractionFeatures)
            .where(InteractionFeatures.tenant_id == tenant.id)
        )
        assert (await session.execute(feat_stmt)).scalar_one() == 8


async def test_seeder_is_idempotent_on_full_tenant(
    test_session_factory, seeded_tenant
):
    """A second pass on a fully-seeded tenant adds nothing."""
    tenant, admin = seeded_tenant
    async with test_session_factory() as session:
        await seed_demo_data(
            session,
            tenant=await session.get(Tenant, tenant.id),
            admin_user=await session.get(User, admin.id),
        )

    async with test_session_factory() as session:
        counts = await seed_demo_data(
            session,
            tenant=await session.get(Tenant, tenant.id),
            admin_user=await session.get(User, admin.id),
        )

    # Every category should report zero — interactions short-circuited
    # by the "already seeded" check, others by per-row dedupe.
    assert counts == {
        "scorecards": 0,
        "interactions": 0,
        "action_items": 0,
        "kb_docs": 0,
        "webhooks": 0,
    }
    # Row totals stay at the first-pass numbers.
    async with test_session_factory() as session:
        assert await _count(session, ScorecardTemplate, tenant.id) == 2
        assert await _count(session, Interaction, tenant.id) == 8
        assert await _count(session, ActionItem, tenant.id) == 6
        assert await _count(session, KBDocument, tenant.id) == 2
        assert await _count(session, Webhook, tenant.id) == 1


async def test_seeder_tops_up_missing_artifacts(
    test_session_factory, seeded_tenant
):
    """If a tenant has interactions but no scorecards / KB docs / webhook,
    a re-run should still backfill those without duplicating interactions.
    """
    tenant, admin = seeded_tenant
    # Plant one bare interaction so the "already seeded" check trips,
    # but no scorecard / kb / webhook.
    async with test_session_factory() as session:
        session.add(
            Interaction(
                tenant_id=tenant.id,
                channel="voice",
                source="manual",
                title="pre-existing call",
                status="completed",
                engine="demo",
            )
        )
        await session.commit()

    async with test_session_factory() as session:
        counts = await seed_demo_data(
            session,
            tenant=await session.get(Tenant, tenant.id),
            admin_user=await session.get(User, admin.id),
        )

    # No new interactions / action items (already-seeded short-circuit),
    # but scorecards + KB + webhook fill in.
    assert counts["interactions"] == 0
    assert counts["action_items"] == 0
    assert counts["scorecards"] == 2
    assert counts["kb_docs"] == 2
    assert counts["webhooks"] == 1
