"""Grace-period re-attach for live media sessions (challenge #2c).

A dirty WS disconnect must not immediately finalize the call: the
session gets a grace window in which a reconnect to the same session
URL resumes the timeline (connection generation + audio position in
Redis). Only if nobody re-attaches does the deferred finalizer dispatch
batch analysis. Clean stops finalize immediately.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from backend.app.services.telephony.live_session_resume import (
    begin_connection,
    clear_resume_state,
    record_audio_position,
    schedule_deferred_finalize,
)


class FakeRedis:
    """In-memory async Redis covering incr/get/set/expire/delete."""

    def __init__(self):
        self.store: Dict[str, str] = {}
        self.closed = False

    async def incr(self, key):
        val = int(self.store.get(key, "0")) + 1
        self.store[key] = str(val)
        return val

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def expire(self, key, ttl):
        return True

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
        return len(keys)

    async def aclose(self):
        self.closed = True


# ── Attach bookkeeping ─────────────────────────────────────────────────


def test_first_connection_is_fresh():
    async def scenario():
        r = FakeRedis()
        attempt = await begin_connection(r, "sess-1")
        assert attempt.generation == 1
        assert attempt.resume_offset_sec == 0.0
        assert attempt.resumed is False

    asyncio.run(scenario())


def test_reattach_resumes_from_recorded_position():
    async def scenario():
        r = FakeRedis()
        await begin_connection(r, "sess-1")
        await record_audio_position(r, "sess-1", 123.4)
        attempt = await begin_connection(r, "sess-1")
        assert attempt.generation == 2
        assert attempt.resume_offset_sec == 123.4
        assert attempt.resumed is True

    asyncio.run(scenario())


def test_clear_resume_state_resets_everything():
    async def scenario():
        r = FakeRedis()
        await begin_connection(r, "sess-1")
        await record_audio_position(r, "sess-1", 10.0)
        await clear_resume_state(r, "sess-1")
        attempt = await begin_connection(r, "sess-1")
        assert attempt.generation == 1
        assert attempt.resumed is False

    asyncio.run(scenario())


# ── Deferred finalization ──────────────────────────────────────────────


def _finalizer_env():
    r = FakeRedis()
    dispatched: List[str] = []

    async def dispatch(redis, session_id):
        dispatched.append(session_id)

    return r, dispatched, dispatch


def test_finalizes_when_no_reattach_within_grace():
    async def scenario():
        r, dispatched, dispatch = _finalizer_env()
        attempt = await begin_connection(r, "sess-1")
        await record_audio_position(r, "sess-1", 5.0)
        task = schedule_deferred_finalize(
            session_id="sess-1",
            generation=attempt.generation,
            redis_factory=lambda: r,
            dispatch=dispatch,
            grace_sec=0.05,
        )
        await asyncio.wait_for(task, timeout=2)
        assert dispatched == ["sess-1"]
        # State cleaned — the next attach is a fresh call.
        assert await r.get("live:sess-1:conn_gen") is None

    asyncio.run(scenario())


def test_reattach_within_grace_cancels_finalization():
    async def scenario():
        r, dispatched, dispatch = _finalizer_env()
        attempt = await begin_connection(r, "sess-1")
        task = schedule_deferred_finalize(
            session_id="sess-1",
            generation=attempt.generation,
            redis_factory=lambda: r,
            dispatch=dispatch,
            grace_sec=0.1,
        )
        # Re-attach before the grace window elapses.
        await asyncio.sleep(0.02)
        second = await begin_connection(r, "sess-1")
        assert second.generation == 2

        await asyncio.wait_for(task, timeout=2)
        assert dispatched == []

    asyncio.run(scenario())


def test_already_cleared_state_skips_finalization():
    """A provider-side hangup path (e.g. Telnyx webhook) may finalize
    and clear state first — the deferred finalizer must stand down."""

    async def scenario():
        r, dispatched, dispatch = _finalizer_env()
        attempt = await begin_connection(r, "sess-1")
        task = schedule_deferred_finalize(
            session_id="sess-1",
            generation=attempt.generation,
            redis_factory=lambda: r,
            dispatch=dispatch,
            grace_sec=0.05,
        )
        await clear_resume_state(r, "sess-1")
        await asyncio.wait_for(task, timeout=2)
        assert dispatched == []

    asyncio.run(scenario())


# ── Timeline rebase plumbing ───────────────────────────────────────────


def test_window_start_offset_shifts_chunk_offsets():
    from backend.app.services.paralinguistics_live import LiveParalinguisticWindow

    w = LiveParalinguisticWindow(start_offset=100.0)
    w.feed(b"\xff" * 160)
    assert w._chunks[0].offset >= 100.0


def test_pump_accumulates_audio_position_across_offers():
    from backend.app.services.telephony.media_stream_pump import MediaStreamPump

    async def scenario():
        async def send(a: bytes) -> None:
            pass

        pump = MediaStreamPump(send_audio=send, initial_audio_seconds=100.0)
        # 50 frames × 160 bytes at 8000 B/s = 1.0 s of audio.
        for i in range(50):
            pump.offer(b"\xff" * 160)
        assert abs(pump.audio_seconds - 101.0) < 1e-6

    asyncio.run(scenario())
