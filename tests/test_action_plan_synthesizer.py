"""Synthesizer-level pure-logic tests.

The synthesizer itself does several LLM calls — those are exercised in
the staging environment, not here. This file covers the helper logic
that needs deterministic guardrails:

* ``resolve_domain`` priority: forced > triage(>=0.8) > user > tenant.
* The render helpers that build prompt blocks from typed inputs.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from backend.app.models import Tenant, User
from backend.app.services.action_plan.external_context import (
    PROVIDER_CAPABILITIES,
    build_capabilities_block,
)
from backend.app.services.action_plan.synthesizer import resolve_domain
from backend.app.services.triage_service import (
    DOMAIN_OVERRIDE_CONFIDENCE_THRESHOLD,
)


@pytest_asyncio.fixture
async def tenant_and_user(test_session_factory):
    async with test_session_factory() as session:
        tenant = Tenant(
            name="t",
            slug=f"t-{uuid.uuid4().hex[:8]}",
            default_domain="sales",
        )
        session.add(tenant)
        await session.flush()
        user = User(
            tenant_id=tenant.id,
            email="user@example.com",
            role="agent",
            default_domain=None,
        )
        session.add(user)
        await session.commit()
        return {
            "session_factory": test_session_factory,
            "tenant_id": tenant.id,
            "user_id": user.id,
        }


# ──────────────────────────────────────────────────────────
# resolve_domain — locked priority order
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_domain_forced_wins(tenant_and_user):
    factory = tenant_and_user["session_factory"]
    async with factory() as db:
        tenant = await db.get(Tenant, tenant_and_user["tenant_id"])
        domain, source = await resolve_domain(
            db,
            tenant=tenant,
            acting_user_id=tenant_and_user["user_id"],
            triage={
                "domain_prediction": {
                    "domain": "customer_service",
                    "confidence": 0.99,
                },
            },
            forced_domain="it_support",
        )
    assert domain == "it_support"
    assert source == "forced"


@pytest.mark.asyncio
async def test_resolve_domain_triage_override_when_confidence_above_threshold(
    tenant_and_user,
):
    factory = tenant_and_user["session_factory"]
    async with factory() as db:
        tenant = await db.get(Tenant, tenant_and_user["tenant_id"])
        domain, source = await resolve_domain(
            db,
            tenant=tenant,
            acting_user_id=tenant_and_user["user_id"],
            triage={
                "domain_prediction": {
                    "domain": "customer_service",
                    "confidence": DOMAIN_OVERRIDE_CONFIDENCE_THRESHOLD + 0.05,
                },
            },
            forced_domain=None,
        )
    assert domain == "customer_service"
    assert source == "triage_override"


@pytest.mark.asyncio
async def test_resolve_domain_triage_ignored_when_confidence_below_threshold(
    tenant_and_user,
):
    """Locked: confidence must be >= 0.8. Anything below uses
    user/tenant default."""
    factory = tenant_and_user["session_factory"]
    async with factory() as db:
        tenant = await db.get(Tenant, tenant_and_user["tenant_id"])
        # User has no default_domain -> tenant default ('sales') wins.
        domain, source = await resolve_domain(
            db,
            tenant=tenant,
            acting_user_id=tenant_and_user["user_id"],
            triage={
                "domain_prediction": {
                    "domain": "customer_service",
                    "confidence": DOMAIN_OVERRIDE_CONFIDENCE_THRESHOLD - 0.05,
                },
            },
            forced_domain=None,
        )
    assert domain == "sales"
    assert source == "tenant_default"


@pytest.mark.asyncio
async def test_resolve_domain_user_default_overrides_tenant_default(
    tenant_and_user,
):
    factory = tenant_and_user["session_factory"]
    async with factory() as db:
        user = await db.get(User, tenant_and_user["user_id"])
        user.default_domain = "it_support"
        await db.commit()

    async with factory() as db:
        tenant = await db.get(Tenant, tenant_and_user["tenant_id"])
        domain, source = await resolve_domain(
            db,
            tenant=tenant,
            acting_user_id=tenant_and_user["user_id"],
            triage={},
            forced_domain=None,
        )
    assert domain == "it_support"
    assert source == "team_default"


@pytest.mark.asyncio
async def test_resolve_domain_falls_back_to_generic_when_tenant_default_invalid(
    tenant_and_user,
):
    """Defensive: a malformed tenant.default_domain shouldn't crash
    synthesis — fall back to generic."""
    factory = tenant_and_user["session_factory"]
    async with factory() as db:
        tenant = await db.get(Tenant, tenant_and_user["tenant_id"])
        tenant.default_domain = "not_a_real_domain"
        await db.commit()
    async with factory() as db:
        tenant = await db.get(Tenant, tenant_and_user["tenant_id"])
        domain, source = await resolve_domain(
            db,
            tenant=tenant,
            acting_user_id=None,
            triage={},
            forced_domain=None,
        )
    assert domain == "generic"


@pytest.mark.asyncio
async def test_resolve_domain_no_user_uses_tenant_default(tenant_and_user):
    factory = tenant_and_user["session_factory"]
    async with factory() as db:
        tenant = await db.get(Tenant, tenant_and_user["tenant_id"])
        domain, source = await resolve_domain(
            db,
            tenant=tenant,
            acting_user_id=None,
            triage={},
            forced_domain=None,
        )
    assert domain == "sales"
    assert source == "tenant_default"


# ──────────────────────────────────────────────────────────
# build_capabilities_block — what Call A sees about integrations
# ──────────────────────────────────────────────────────────


def test_build_capabilities_block_no_providers_disallows_system_writes():
    block = build_capabilities_block([])
    lowered = block.lower()
    assert "do not emit" in lowered or "log manually" in lowered


def test_build_capabilities_block_lists_each_provider():
    block = build_capabilities_block(["hubspot", "salesforce"])
    assert "hubspot" in block
    assert "salesforce" in block


def test_build_capabilities_block_marks_unknown_provider():
    """A provider connected but not in PROVIDER_CAPABILITIES still
    appears in the block (so the LLM knows it exists) but with an
    'unknown' marker so Call A doesn't fabricate operations."""
    block = build_capabilities_block(["totally_new_provider"])
    assert "totally_new_provider" in block
    assert "unknown" in block.lower()


def test_provider_capabilities_only_lists_supported_adapters():
    """Each provider in PROVIDER_CAPABILITIES must correspond to an
    adapter we actually support — otherwise the LLM would recommend
    system_write steps the engine can't execute."""
    # Today's CRM adapters: hubspot, salesforce, pipedrive.
    # Plus email/calendar surfaces from existing oauth: gmail, outlook,
    # google_calendar. Adding new entries here is intentional.
    expected = {
        "hubspot", "salesforce", "pipedrive",
        "gmail", "outlook", "google_calendar",
    }
    assert set(PROVIDER_CAPABILITIES.keys()) == expected
