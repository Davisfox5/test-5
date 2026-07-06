"""Exactly-once semantics for the paid analysis step (3a, increment 2).

``run_analysis_with_ledger`` is what step 9 of ``_run_pipeline_impl``
calls instead of the old content-sniffing reuse guard
(``len(summary) >= 40``). Contract:

* ACQUIRED  → run the paid ``analyze_fn``; persist-after-pay: the output
  lands on ``interaction.insights`` in the SAME commit that flips the
  ledger row to ``succeeded``.
* REUSED    → never call ``analyze_fn``; return the persisted insights
  with transient failure keys (``error``/``step``/``retry_count``)
  stripped — a later-step error stamp must not force a re-pay.
* HELD      → raise ``StepHeldError`` so the Celery task defers instead
  of double-paying while another worker is mid-analysis.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import tests.db_fixtures  # noqa: F401  (sqlite JSONB/UUID compilers)

from backend.app.db import Base
from backend.app.models import Interaction, InteractionStepRun, Tenant
from backend.app.services.pipeline_ledger import (
    STEP_ANALYSIS,
    StepHeldError,
    claim_step,
    complete_step,
    run_analysis_with_ledger,
)


@pytest.fixture()
def env(tmp_path):
    url = f"sqlite:///{tmp_path}/exactly_once.db"
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    session = factory()
    tenant = Tenant(id=uuid.uuid4(), name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    ix = Interaction(id=uuid.uuid4(), tenant_id=tenant.id, channel="voice")
    session.add(ix)
    session.commit()
    tid, iid = tenant.id, ix.id
    session.close()
    try:
        yield factory, tid, iid
    finally:
        engine.dispose()


def _run(factory, tid, iid, analyze_fn, *, input_hash="h1", worker="w1"):
    session = factory()
    try:
        ix = session.get(Interaction, iid)
        return run_analysis_with_ledger(
            session,
            tenant_id=tid,
            interaction=ix,
            input_hash=input_hash,
            worker_id=worker,
            analyze_fn=analyze_fn,
        )
    finally:
        session.close()


def test_acquired_runs_and_persists_after_pay(env):
    factory, tid, iid = env
    calls: List[int] = []

    def analyze():
        calls.append(1)
        return {"summary": "A perfectly thorough summary of the call.", "sentiment_score": 0.4}

    insights = _run(factory, tid, iid, analyze)
    assert len(calls) == 1
    assert insights["summary"].startswith("A perfectly")

    # Persisted: a fresh session sees insights on the row AND a
    # succeeded ledger run — the same-commit persist-after-pay contract.
    session = factory()
    row = session.get(Interaction, iid)
    assert (row.insights or {}).get("summary", "").startswith("A perfectly")
    run = session.query(InteractionStepRun).filter_by(interaction_id=iid).one()
    assert run.status == "succeeded"
    session.close()


def test_reused_skips_paid_call_and_strips_error_stamp(env):
    factory, tid, iid = env

    first = _run(factory, tid, iid, lambda: {"summary": "The original paid summary text."})
    assert first["summary"].startswith("The original")

    # Simulate a later-step failure stamping the row (what the task's
    # failure handler does) — this must NOT force a re-pay.
    session = factory()
    ix = session.get(Interaction, iid)
    stamped = dict(ix.insights or {})
    stamped.update({"error": "ValueError: boom", "step": "voice_pipeline", "retry_count": 1})
    ix.insights = stamped
    session.commit()
    session.close()

    def explode():
        raise AssertionError("paid call must not happen on REUSED")

    reused = _run(factory, tid, iid, explode)
    assert reused["summary"].startswith("The original")
    assert "error" not in reused
    assert "step" not in reused
    assert "retry_count" not in reused


def test_held_raises_instead_of_double_paying(env):
    factory, tid, iid = env
    session = factory()
    claim = claim_step(
        session, tenant_id=tid, interaction_id=iid, step_key=STEP_ANALYSIS,
        input_hash="h1", worker_id="other-worker",
    )
    session.close()
    assert claim.outcome == "acquired"

    def explode():
        raise AssertionError("paid call must not happen on HELD")

    with pytest.raises(StepHeldError):
        _run(factory, tid, iid, explode)


def test_failed_analysis_is_retryable(env):
    factory, tid, iid = env

    def boom():
        raise RuntimeError("anthropic 500")

    with pytest.raises(RuntimeError):
        _run(factory, tid, iid, boom)

    session = factory()
    run = session.query(InteractionStepRun).filter_by(interaction_id=iid).one()
    assert run.status == "failed"
    assert "anthropic 500" in (run.error or "")
    session.close()

    ok = _run(factory, tid, iid, lambda: {"summary": "Second attempt succeeded fine."})
    assert ok["summary"].startswith("Second attempt")


def test_changed_input_reanalyzes(env):
    factory, tid, iid = env
    calls: List[str] = []

    def make(tag):
        def analyze():
            calls.append(tag)
            return {"summary": f"Summary generated for input {tag}."}
        return analyze

    _run(factory, tid, iid, make("v1"), input_hash="hash-v1")
    _run(factory, tid, iid, make("v2"), input_hash="hash-v2")
    assert calls == ["v1", "v2"]


def test_concurrent_workers_pay_exactly_once(env):
    """The 3a double-charge race, end to end: N workers race the same
    interaction+input. Exactly one pays; the rest defer (HELD) or reuse."""
    factory, tid, iid = env
    n = 6
    paid: List[int] = []
    outcomes: List[str] = [""] * n
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def analyze_slow():
        with lock:
            paid.append(1)
        time.sleep(0.4)  # hold the claim long enough for racers to observe it
        return {"summary": "The one and only paid analysis output."}

    def worker(i: int) -> None:
        barrier.wait()
        try:
            insights = _run(factory, tid, iid, analyze_slow, worker=f"w{i}")
            outcomes[i] = "got:" + insights["summary"][:7]
        except StepHeldError:
            outcomes[i] = "held"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(paid) == 1, outcomes
    # Everyone either deferred or saw the winner's persisted output.
    assert all(o == "held" or o.startswith("got:") for o in outcomes)
