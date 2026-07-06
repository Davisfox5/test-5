"""3b — event-loop × asyncpg-pool coupling (docs/complexity/01, inc 5).

The structural fix: ``_TaskEventLoop.__enter__`` disposes the shared
async engine's pool once, inside the newly created loop, so EVERY
``_loop.run(...)`` call site in the pipeline (webhook lifecycle emit,
plan synthesis, search indexing, …) gets connections bound to the
task's own loop by construction — instead of each call site having to
remember the dispose-first convention (``_emit_lifecycle`` didn't, and
its stale-loop failures were silently swallowed).
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

import pytest

import backend.app.tasks as tasks
import backend.app.db as appdb


class RecordingEngine:
    """Stands in for the module-level async engine; records which loop
    each dispose() ran on."""

    def __init__(self) -> None:
        self.dispose_loops: List[asyncio.AbstractEventLoop] = []

    async def dispose(self) -> None:
        self.dispose_loops.append(asyncio.get_event_loop())


def test_task_loop_entry_disposes_engine_in_new_loop(monkeypatch):
    rec = RecordingEngine()
    monkeypatch.setattr(appdb, "engine", rec)

    with tasks._TaskEventLoop() as loop:
        # Disposed exactly once, and inside THIS task's fresh loop —
        # so subsequent checkouts bind to it.
        assert len(rec.dispose_loops) == 1
        assert rec.dispose_loops[0] is loop._loop
        # The loop is usable for normal work afterwards.
        assert loop.run(asyncio.sleep(0, result=42)) == 42

    assert len(rec.dispose_loops) == 1


def test_sequential_task_loops_each_dispose(monkeypatch):
    """Two back-to-back Celery tasks on the same prefork child: each
    task's loop must start from a disposed pool, so connections opened
    by task 1 can never be checked out under task 2's loop."""
    rec = RecordingEngine()
    monkeypatch.setattr(appdb, "engine", rec)

    with tasks._TaskEventLoop() as l1:
        first_loop = l1._loop
        l1.run(asyncio.sleep(0))
    with tasks._TaskEventLoop() as l2:
        second_loop = l2._loop
        l2.run(asyncio.sleep(0))

    assert rec.dispose_loops == [first_loop, second_loop]
    assert first_loop is not second_loop


def test_dispose_failure_does_not_kill_the_task(monkeypatch):
    """A dispose blip must not fail the pipeline — the engine might be
    mid-teardown or unconfigured in a given environment."""

    class ExplodingEngine:
        async def dispose(self) -> None:
            raise RuntimeError("pool already torn down")

    monkeypatch.setattr(appdb, "engine", ExplodingEngine())

    with tasks._TaskEventLoop() as loop:
        assert loop.run(asyncio.sleep(0, result="ok")) == "ok"
