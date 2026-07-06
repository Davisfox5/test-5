"""3c — orphan reconciliation sweeper (docs/complexity/01, inc 6).

Entity resolution is best-effort in the pipeline: a failure lands the
interaction as ``analyzed`` with no customer linkage. The ledger makes
that state discoverable (a ``failed`` entity_resolution run), and
``reconcile_orphan_interactions`` heals it: re-claim the failed run
(atomic — a racing sweeper or pipeline retry loses cleanly) and re-run
resolution against the persisted insights.

A ``succeeded`` run with no linkage means "genuinely nobody to resolve"
and must NOT be retried — that's the crash-vs-nobody distinction the
ledger exists to make.
"""

from __future__ import annotations

import sys
import types
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import tests.db_fixtures  # noqa: F401

from backend.app.db import Base
from backend.app.models import Interaction, InteractionStepRun, Tenant
import backend.app.tasks as tasks


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/orphans.db"
    engine = create_engine(url, poolclass=NullPool, connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    session = factory()
    tenant = Tenant(id=uuid.uuid4(), name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    session.commit()
    tid = tenant.id
    session.close()

    monkeypatch.setattr(tasks, "_get_sync_session", lambda: factory())

    resolved: List[str] = []

    async def fake_resolve(**kw):
        ix = kw["interaction"]
        resolved.append(str(ix.id))
        return SimpleNamespace(
            customer_action="linked", customer_score=0.9,
            customer_id=uuid.uuid4(), suggestions=None,
        )

    er_stub = types.ModuleType("backend.app.services.entity_resolution")
    er_stub.resolve_interaction_entities = fake_resolve
    monkeypatch.setitem(
        sys.modules, "backend.app.services.entity_resolution", er_stub
    )

    def seed_orphan(er_status: Optional[str] = "failed") -> uuid.UUID:
        session = factory()
        ix = Interaction(
            id=uuid.uuid4(), tenant_id=tid, channel="voice", status="analyzed",
            transcript=[{"start": 0, "end": 3, "text": "hello", "speaker_id": "rep"}],
            insights={"summary": "A long enough summary for reuse checks."},
        )
        session.add(ix)
        session.flush()
        if er_status is not None:
            session.add(
                InteractionStepRun(
                    tenant_id=tid, interaction_id=ix.id,
                    step_key="entity_resolution", input_hash="h-er",
                    status=er_status, attempt=1,
                )
            )
        session.commit()
        iid = ix.id
        session.close()
        return iid

    yield SimpleNamespace(
        factory=factory, tenant_id=tid, seed_orphan=seed_orphan, resolved=resolved
    )
    engine.dispose()


def test_failed_resolution_orphan_is_healed(env):
    iid = env.seed_orphan(er_status="failed")

    result = tasks.reconcile_orphan_interactions()

    assert result["healed"] == 1
    assert env.resolved == [str(iid)]
    session = env.factory()
    run = (
        session.query(InteractionStepRun)
        .filter_by(interaction_id=iid, step_key="entity_resolution")
        .one()
    )
    assert run.status == "succeeded"
    assert run.attempt == 2  # takeover of the failed run, not a new row
    session.close()


def test_succeeded_resolution_is_left_alone(env):
    """No linkage + succeeded run = 'genuinely nobody' — never re-run."""
    env.seed_orphan(er_status="succeeded")

    result = tasks.reconcile_orphan_interactions()

    assert result["healed"] == 0
    assert env.resolved == []


def test_still_failing_orphan_stays_failed_and_does_not_kill_batch(env):
    bad = env.seed_orphan(er_status="failed")
    good = env.seed_orphan(er_status="failed")

    async def sometimes_resolve(**kw):
        ix = kw["interaction"]
        if ix.id == bad:
            raise RuntimeError("still broken")
        env.resolved.append(str(ix.id))
        return SimpleNamespace(
            customer_action="linked", customer_score=0.9,
            customer_id=uuid.uuid4(), suggestions=None,
        )

    sys.modules["backend.app.services.entity_resolution"].resolve_interaction_entities = (
        sometimes_resolve
    )

    result = tasks.reconcile_orphan_interactions()

    assert result["healed"] == 1
    assert result["failed"] == 1
    session = env.factory()
    bad_run = (
        session.query(InteractionStepRun)
        .filter_by(interaction_id=bad, step_key="entity_resolution")
        .one()
    )
    assert bad_run.status == "failed"  # retryable on the next sweep
    good_run = (
        session.query(InteractionStepRun)
        .filter_by(interaction_id=good, step_key="entity_resolution")
        .one()
    )
    assert good_run.status == "succeeded"
    session.close()


def test_batch_size_is_respected(env):
    for _ in range(5):
        env.seed_orphan(er_status="failed")

    result = tasks.reconcile_orphan_interactions(batch_size=2)

    assert result["healed"] == 2
    assert result["scanned"] == 2


def test_scheduled_in_beat(env):
    entries = tasks.celery_app.conf.beat_schedule
    assert any(
        e.get("task") == "reconcile_orphan_interactions" for e in entries.values()
    )
