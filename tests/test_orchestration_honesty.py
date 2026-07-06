"""3d — honest, non-blocking orchestration (docs/complexity/01, inc 7).

Before: the daily/weekly orchestrators dispatched a chord and then
sync-waited on it inside the Celery task (`.get(timeout=3600)`) —
pinning a worker slot for up to an hour, and on timeout reporting
``tenants_processed: 0`` with every tenant marked failed even when the
per-tenant subtasks all succeeded. The heavy scans ran tenants
sequentially under one shared sync session.

After: dispatchers return immediately with a dispatch receipt; the
chord callback is the single honest aggregate (it sees real per-tenant
outcomes and logs them); scans fan out per-tenant subtasks, each owning
its session and event loop.
"""

from __future__ import annotations

import sys
import types
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import celery as celery_module
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import tests.db_fixtures  # noqa: F401

from backend.app.db import Base
from backend.app.models import Tenant
import backend.app.tasks as tasks


class FakeChordResult:
    """What a dispatched chord hands back. Deliberately has NO ``.get``:
    if a dispatcher still tries to sync-wait, the test explodes."""

    def __init__(self) -> None:
        self.id = "fake-chord-id"

    def __getattr__(self, name):
        if name == "get":
            raise AssertionError("dispatcher must not sync-wait on the chord")
        raise AttributeError(name)


class FakeChord:
    dispatched: List[Dict[str, Any]] = []

    def __init__(self, header) -> None:
        self.header = list(header)

    def __call__(self, callback):
        FakeChord.dispatched.append(
            {"header_size": len(self.header), "callback": callback}
        )
        return FakeChordResult()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/orch.db"
    engine = create_engine(url, poolclass=NullPool, connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    session = factory()
    tids = []
    for i in range(3):
        t = Tenant(id=uuid.uuid4(), name=f"T{i}", slug=f"t{i}-{uuid.uuid4().hex[:6]}")
        session.add(t)
        tids.append(t.id)
    session.commit()
    session.close()

    monkeypatch.setattr(tasks, "_get_sync_session", lambda: factory())
    monkeypatch.setattr(celery_module, "chord", FakeChord)
    monkeypatch.setattr(celery_module, "group", lambda sigs: list(sigs))
    FakeChord.dispatched = []

    yield SimpleNamespace(factory=factory, tenant_ids=tids)
    engine.dispose()


# ── dispatchers return immediately ────────────────────────────────────────

def test_daily_orchestrator_dispatches_without_blocking(env):
    result = tasks.orchestrator_daily_all_tenants()
    assert result == {"dispatched_tenants": 3, "chord_id": "fake-chord-id"}
    assert FakeChord.dispatched[0]["header_size"] == 3


def test_weekly_orchestrator_dispatches_without_blocking(env):
    result = tasks.orchestrator_weekly_all_tenants()
    assert result == {"dispatched_tenants": 3, "chord_id": "fake-chord-id"}


def test_support_trend_scan_dispatches_per_tenant(env):
    result = tasks.support_trend_scan()
    assert result == {"dispatched_tenants": 3, "chord_id": "fake-chord-id"}
    assert FakeChord.dispatched[0]["header_size"] == 3


def test_cohort_scan_dispatches_per_tenant(env):
    result = tasks.cohort_recommendation_scan()
    assert result == {"dispatched_tenants": 3, "chord_id": "fake-chord-id"}


# ── the aggregate is honest ───────────────────────────────────────────────

def test_aggregate_orchestration_reports_real_outcomes():
    results = [
        {"success": True, "tenant_id": "a", "totals": {"x": 2}, "baseline_refreshed": True},
        {"success": True, "tenant_id": "b", "totals": {"x": 1}},
        {"success": False, "tenant_id": "c"},
    ]
    agg = tasks._aggregate_orchestration(results)
    assert agg["tenants_processed"] == 2
    assert agg["profile_updates"] == {"x": 3}
    assert agg["paralinguistic_baselines_refreshed"] == 1
    assert agg["failed_tenants"] == ["c"]


def test_log_scan_aggregate_counts_and_names_failures():
    results = [
        {"tenant_id": "a", "clusters": 2},
        {"tenant_id": "b", "error": 1},
        None,  # a subtask that died entirely
    ]
    agg = tasks._log_scan_aggregate(results, "support_trend_scan")
    assert agg["scan"] == "support_trend_scan"
    assert agg["tenants_processed"] == 1
    assert "b" in agg["failed_tenants"]
    assert len(agg["failed_tenants"]) == 2


# ── per-tenant subtasks own their session ─────────────────────────────────

def test_support_trend_subtask_uses_its_own_session(env, monkeypatch):
    sessions_made: List[Any] = []
    real_factory = env.factory

    def counting_factory():
        s = real_factory()
        sessions_made.append(s)
        return s

    monkeypatch.setattr(tasks, "_get_sync_session", counting_factory)

    seen: List[str] = []

    async def fake_run_for_tenant(session, tenant):
        seen.append(str(tenant.id))
        return {"clusters": 1}

    std_stub = types.ModuleType("backend.app.services.support_trend_detector")
    std_stub.run_for_tenant = fake_run_for_tenant
    monkeypatch.setitem(
        sys.modules, "backend.app.services.support_trend_detector", std_stub
    )

    tid = str(env.tenant_ids[0])
    result = tasks.support_trend_scan_tenant(tid)

    assert result["tenant_id"] == tid
    assert result["clusters"] == 1
    assert seen == [tid]
    assert len(sessions_made) == 1  # its own session, not a shared one


def test_cohort_subtask_survives_tenant_failure(env, monkeypatch):
    def exploding_run_for_tenant(session, tenant):
        raise RuntimeError("detector blew up")

    cr_stub = types.ModuleType("backend.app.services.cohort_recommendations")
    cr_stub.run_for_tenant = exploding_run_for_tenant
    monkeypatch.setitem(
        sys.modules, "backend.app.services.cohort_recommendations", cr_stub
    )

    tid = str(env.tenant_ids[0])
    result = tasks.cohort_recommendation_scan_tenant(tid)
    assert result["tenant_id"] == tid
    assert result["error"] == 1
