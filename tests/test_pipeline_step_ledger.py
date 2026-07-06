"""Tests for the durable per-step run ledger (docs/complexity/01, increment 1).

The ledger is the exactly-once backbone for the interaction pipeline:
each paid / non-idempotent step claims a row keyed on
(interaction_id, step_key, input_hash) before running. The claim must be
atomic under concurrency — two racing workers must produce exactly one
ACQUIRED — and a completed run must be reusable so retries never re-pay
an LLM call whose output already landed.

Runs against sync SQLAlchemy sessions (the pipeline is sync-Celery) over
a file-backed SQLite DB so the thread-race test exercises real separate
connections. SQLite serializes writes, but the unique-constraint /
IntegrityError semantics the claim relies on are identical to Postgres.
"""

from __future__ import annotations

import threading
import uuid
from typing import List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

# Registers the sqlite compilers for JSONB / UUID (global side effect).
import tests.db_fixtures  # noqa: F401

from backend.app.db import Base
from backend.app.models import Interaction, InteractionStepRun, Tenant
from backend.app.services.pipeline_ledger import (
    STEP_ANALYSIS,
    STEP_ENTITY_RESOLUTION,
    StepClaim,
    claim_step,
    complete_step,
    compute_input_hash,
    fail_step,
)


@pytest.fixture()
def sync_db(tmp_path):
    """File-backed SQLite engine + session factory (thread-safe-ish)."""
    url = f"sqlite:///{tmp_path}/ledger.db"
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def seeded(sync_db):
    """Seed a tenant + interaction; return (factory, tenant_id, interaction_id)."""
    session = sync_db()
    tenant = Tenant(id=uuid.uuid4(), name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    ix = Interaction(id=uuid.uuid4(), tenant_id=tenant.id, channel="voice")
    session.add(ix)
    session.commit()
    tid, iid = tenant.id, ix.id
    session.close()
    return sync_db, tid, iid


def _claim(factory, tid, iid, *, step=STEP_ANALYSIS, input_hash="h1",
           worker="w1", lease_seconds=900):
    session = factory()
    try:
        return claim_step(
            session,
            tenant_id=tid,
            interaction_id=iid,
            step_key=step,
            input_hash=input_hash,
            worker_id=worker,
            lease_seconds=lease_seconds,
        )
    finally:
        session.close()


# ── input hash ────────────────────────────────────────────────────────────

def test_input_hash_is_stable_and_order_sensitive():
    a = compute_input_hash("transcript", "variant-a", "sonnet")
    assert a == compute_input_hash("transcript", "variant-a", "sonnet")
    assert a != compute_input_hash("transcript", "variant-b", "sonnet")
    assert a != compute_input_hash("variant-a", "transcript", "sonnet")
    # None is distinguishable from the string "None" / empty
    assert compute_input_hash(None) != compute_input_hash("None")
    assert compute_input_hash(None) != compute_input_hash("")
    assert len(a) == 64  # sha256 hex


# ── claim lifecycle ───────────────────────────────────────────────────────

def test_first_claim_acquires(seeded):
    factory, tid, iid = seeded
    claim = _claim(factory, tid, iid)
    assert claim.outcome == StepClaim.ACQUIRED
    assert claim.run_id is not None
    assert claim.attempt == 1


def test_completed_run_is_reused_not_reacquired(seeded):
    factory, tid, iid = seeded
    claim = _claim(factory, tid, iid)
    session = factory()
    complete_step(session, claim.run_id, output_digest="d1")
    session.close()

    again = _claim(factory, tid, iid, worker="w2")
    assert again.outcome == StepClaim.REUSED
    assert again.run_id == claim.run_id
    assert again.output_digest == "d1"


def test_running_unexpired_claim_is_held(seeded):
    factory, tid, iid = seeded
    first = _claim(factory, tid, iid, worker="w1")
    assert first.outcome == StepClaim.ACQUIRED
    second = _claim(factory, tid, iid, worker="w2")
    assert second.outcome == StepClaim.HELD


def test_expired_lease_can_be_taken_over(seeded):
    factory, tid, iid = seeded
    first = _claim(factory, tid, iid, worker="w1", lease_seconds=-1)
    assert first.outcome == StepClaim.ACQUIRED
    takeover = _claim(factory, tid, iid, worker="w2")
    assert takeover.outcome == StepClaim.ACQUIRED
    assert takeover.run_id == first.run_id
    assert takeover.attempt == 2


def test_failed_run_is_retryable(seeded):
    factory, tid, iid = seeded
    claim = _claim(factory, tid, iid)
    session = factory()
    fail_step(session, claim.run_id, error="boom")
    session.close()

    retry = _claim(factory, tid, iid, worker="w2")
    assert retry.outcome == StepClaim.ACQUIRED
    assert retry.attempt == 2
    # error cleared on takeover
    session = factory()
    row = session.get(InteractionStepRun, claim.run_id)
    assert row.status == "running"
    assert row.error is None
    session.close()


def test_changed_input_hash_is_a_new_run(seeded):
    factory, tid, iid = seeded
    c1 = _claim(factory, tid, iid, input_hash="h1")
    session = factory()
    complete_step(session, c1.run_id)
    session.close()

    c2 = _claim(factory, tid, iid, input_hash="h2")
    assert c2.outcome == StepClaim.ACQUIRED
    assert c2.run_id != c1.run_id


def test_steps_are_independent(seeded):
    factory, tid, iid = seeded
    a = _claim(factory, tid, iid, step=STEP_ANALYSIS)
    b = _claim(factory, tid, iid, step=STEP_ENTITY_RESOLUTION)
    assert a.outcome == StepClaim.ACQUIRED
    assert b.outcome == StepClaim.ACQUIRED
    assert a.run_id != b.run_id


# ── the double-charge race ────────────────────────────────────────────────

def test_concurrent_claims_single_winner(seeded):
    """Two+ racing workers on the same (interaction, step, hash) must
    produce exactly one ACQUIRED — the atomic-claim guarantee that kills
    the double-Sonnet-charge race (3a)."""
    factory, tid, iid = seeded
    n = 8
    outcomes: List[Optional[str]] = [None] * n
    barrier = threading.Barrier(n)

    def racer(i: int) -> None:
        barrier.wait()
        claim = _claim(factory, tid, iid, worker=f"w{i}")
        outcomes[i] = claim.outcome

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count(StepClaim.ACQUIRED) == 1, outcomes
    assert all(o in (StepClaim.ACQUIRED, StepClaim.HELD) for o in outcomes)


def test_concurrent_paid_call_happens_exactly_once(seeded):
    """End-to-end shape of the fix: racers claim, only the winner runs
    the paid analyze(); losers observe HELD (defer) or REUSED (skip)."""
    factory, tid, iid = seeded
    n = 6
    paid_calls = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        claim = _claim(factory, tid, iid, worker=f"w{i}")
        if claim.outcome == StepClaim.ACQUIRED:
            with lock:
                paid_calls.append(i)  # the $ Sonnet call
            session = factory()
            complete_step(session, claim.run_id, output_digest="paid")
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(paid_calls) == 1

    # After the winner completed, a late retry reuses instead of paying.
    late = _claim(factory, tid, iid, worker="late")
    assert late.outcome == StepClaim.REUSED
    assert late.output_digest == "paid"
