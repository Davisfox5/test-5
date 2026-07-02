"""Tests for ``POST /experiments`` control/treatment variant validation.

Before this fix, ``create_experiment`` accepted any (or nonexistent)
control/treatment variant ids with no checks — a mismatched-surface
experiment silently corrupted the A/B analysis downstream. These tests
pin the new 422 behavior.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


pytestmark = pytest.mark.asyncio

PREFIX = "/api/v1"


@pytest_asyncio.fixture
async def experiments_app(test_session_factory, test_tenant):
    from fastapi import FastAPI

    from backend.app.auth import get_current_tenant
    from backend.app.db import get_db
    from backend.app.api.experiments import router as experiments_router

    async def _override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _override_get_tenant():
        return test_tenant

    app = FastAPI()
    app.include_router(experiments_router, prefix=PREFIX, tags=["experiments"])
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_tenant] = _override_get_tenant
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def experiments_client(experiments_app):
    transport = ASGITransport(app=experiments_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _make_variant(client, *, surface="analysis", tier=None, channel=None, name=None):
    resp = await client.post(
        f"{PREFIX}/prompt-variants",
        json={
            "name": name or f"v-{uuid.uuid4().hex[:8]}",
            "prompt_template": "hello {{name}}",
            "target_surface": surface,
            "target_tier": tier,
            "target_channel": channel,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_experiment_rejects_mismatched_surface(experiments_client):
    control = await _make_variant(experiments_client, surface="analysis")
    treatment = await _make_variant(experiments_client, surface="email_reply")

    resp = await experiments_client.post(
        f"{PREFIX}/experiments",
        json={
            "name": "surface-mismatch",
            "type": "prompt_variant",
            "control_variant_id": control["id"],
            "treatment_variant_id": treatment["id"],
        },
    )
    assert resp.status_code == 422
    assert "surface" in resp.json()["detail"].lower()


async def test_create_experiment_rejects_unknown_variant(experiments_client):
    control = await _make_variant(experiments_client, surface="analysis")

    resp = await experiments_client.post(
        f"{PREFIX}/experiments",
        json={
            "name": "unknown-variant",
            "type": "prompt_variant",
            "control_variant_id": control["id"],
            "treatment_variant_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 422


async def test_create_experiment_rejects_equal_control_and_treatment(experiments_client):
    control = await _make_variant(experiments_client, surface="analysis")

    resp = await experiments_client.post(
        f"{PREFIX}/experiments",
        json={
            "name": "same-variant",
            "type": "prompt_variant",
            "control_variant_id": control["id"],
            "treatment_variant_id": control["id"],
        },
    )
    assert resp.status_code == 422


async def test_create_experiment_accepts_matching_surface_tier_channel(experiments_client):
    control = await _make_variant(
        experiments_client, surface="email_classifier", tier="pro", channel="email"
    )
    treatment = await _make_variant(
        experiments_client, surface="email_classifier", tier="pro", channel="email"
    )

    resp = await experiments_client.post(
        f"{PREFIX}/experiments",
        json={
            "name": "valid-pair",
            "type": "prompt_variant",
            "control_variant_id": control["id"],
            "treatment_variant_id": treatment["id"],
        },
    )
    assert resp.status_code == 201, resp.text
