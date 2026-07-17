"""3b — event-loop × asyncpg-pool coupling (docs/complexity/01, inc 5).

The structural fix, in two halves (see ``_TaskEventLoop`` /
``_run_async`` in tasks.py):

* ``__enter__`` resets the shared async engine's pool once, inside the
  newly created loop, with ``close=False`` — any connections still
  pooled there belong to an ALREADY-CLOSED loop (a prior task that
  crashed before its exit dispose), and a graceful close from this loop
  is exactly the cross-loop operation that raised "RuntimeError: Event
  loop is closed" (Sentry LINDA-STAGING-2R). Abandoning them to GC is
  the only loop-safe option.
* ``__exit__`` disposes fully, ON the loop that owns the connections,
  BEFORE that loop closes — so in steady state the pool never carries
  a connection across loops at all and the entry reset is a no-op.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

import pytest

import backend.app.tasks as tasks
import backend.app.db as appdb


class RecordingEngine:
    """Stands in for the module-level async engine; records which loop
    each dispose() ran on and whether it was a full close."""

    def __init__(self) -> None:
        self.disposals: List[Tuple[asyncio.AbstractEventLoop, bool]] = []

    async def dispose(self, close: bool = True) -> None:
        self.disposals.append((asyncio.get_event_loop(), close))


def test_task_loop_entry_resets_and_exit_closes_on_own_loop(monkeypatch):
    rec = RecordingEngine()
    monkeypatch.setattr(appdb, "engine", rec)

    with tasks._TaskEventLoop() as loop:
        # Entry: exactly one reset, inside THIS task's fresh loop, and
        # non-closing (stale-loop connections can't be closed from here).
        assert rec.disposals == [(loop._loop, False)]
        task_loop = loop._loop
        # The loop is usable for normal work afterwards.
        assert loop.run(asyncio.sleep(0, result=42)) == 42

    # Exit: one FULL dispose, on the same loop, before it closed — the
    # graceful close happens while the owning loop is still alive.
    assert rec.disposals == [(task_loop, False), (task_loop, True)]


def test_sequential_task_loops_never_share_pooled_connections(monkeypatch):
    """Two back-to-back Celery tasks on the same prefork child: each
    task's loop starts from a reset pool and closes its own connections
    on exit, so connections opened under task 1 can never be checked
    out (or closed) under task 2's loop."""
    rec = RecordingEngine()
    monkeypatch.setattr(appdb, "engine", rec)

    with tasks._TaskEventLoop() as l1:
        first_loop = l1._loop
        l1.run(asyncio.sleep(0))
    with tasks._TaskEventLoop() as l2:
        second_loop = l2._loop
        l2.run(asyncio.sleep(0))

    assert rec.disposals == [
        (first_loop, False),
        (first_loop, True),
        (second_loop, False),
        (second_loop, True),
    ]
    assert first_loop is not second_loop


def test_run_async_resets_then_closes_around_the_tick(monkeypatch):
    """``_run_async`` (the beat-task wrapper): non-closing reset before
    the tick body, full dispose after it — even when the body raises."""
    rec = RecordingEngine()
    monkeypatch.setattr(appdb, "engine", rec)

    async def _body():
        return "done"

    assert tasks._run_async(_body) == "done"
    assert [c for (_, c) in rec.disposals] == [False, True]

    rec.disposals.clear()

    async def _boom():
        raise ValueError("tick failed")

    with pytest.raises(ValueError):
        tasks._run_async(_boom)
    # The full dispose still ran on the tick's own loop (finally).
    assert [c for (_, c) in rec.disposals] == [False, True]


def test_dispose_failure_does_not_kill_the_task(monkeypatch):
    """A dispose blip must not fail the pipeline — the engine might be
    mid-teardown or unconfigured in a given environment."""

    class ExplodingEngine:
        async def dispose(self, close: bool = True) -> None:
            raise RuntimeError("pool already torn down")

    monkeypatch.setattr(appdb, "engine", ExplodingEngine())

    with tasks._TaskEventLoop() as loop:
        assert loop.run(asyncio.sleep(0, result="ok")) == "ok"
