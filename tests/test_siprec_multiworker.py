"""SIPREC bridge durability across uvicorn workers (challenge #2c / C4).

The API runs ``--workers 2`` and the SRS delivers every event as an
independent HTTP POST, so ``recording.started`` and the audio frames
can land on different processes. These tests simulate two workers as
two ``SiprecBridge`` instances sharing one fake Redis and pin:

* frames landing on the worker that never saw ``recording.started``
  are forwarded, not dropped (state read-through + lazy dispatch open);
* concurrent ``recording.started`` retries insert exactly one session
  (atomic claim), in-process and across workers;
* the sequence guard holds across workers;
* ``recording.stopped`` on either worker tears the session down;
* the idle reaper finalises sessions whose Redis state expired.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional, Tuple

from backend.app.services.audio import AudioFormat
from backend.app.services.telephony.siprec.bridge import (
    SiprecAudioFrame,
    SiprecBridge,
)

TENANT = uuid.uuid4()
AGENT = uuid.uuid4()


class FakeRedis:
    """In-memory async Redis: get/set(nx,ex)/delete/hget/hset/expire.

    TTLs are recorded but only enforced via :meth:`expire_key` so tests
    can simulate idle expiry deterministically.
    """

    def __init__(self):
        self.store: Dict[str, Any] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
            self.hashes.pop(k, None)
        return len(keys)

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(str(field))

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[str(field)] = str(value)
        return 1

    async def expire(self, key, ttl):
        return True

    def expire_key(self, key):
        self.store.pop(key, None)
        self.hashes.pop(key, None)


class RecordingDispatch:
    def __init__(self):
        self.opened: List[str] = []
        self.frames: List[Tuple[str, str, bytes]] = []
        self.closed: List[Tuple[str, Optional[str]]] = []

    async def open_session(self, recording_session_id, live_session_id, tenant_id, provider):
        self.opened.append(recording_session_id)

    async def send_audio(self, recording_session_id, label, audio_mulaw_8k):
        self.frames.append((recording_session_id, label, audio_mulaw_8k))

    async def close_session(self, recording_session_id, reason=None):
        self.closed.append((recording_session_id, reason))


class CountingBridge(SiprecBridge):
    """Counts DB inserts without a DB (session_factory=None mints a
    uuid; we intercept to count how many times an insert happened)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.persist_started_calls = 0

    async def _persist_started(self, **kwargs):
        self.persist_started_calls += 1
        return uuid.uuid4()


def _two_workers(redis: FakeRedis) -> Tuple[CountingBridge, CountingBridge, RecordingDispatch, RecordingDispatch]:
    d_a, d_b = RecordingDispatch(), RecordingDispatch()
    a = CountingBridge(dispatch=d_a, redis_factory=lambda: redis)
    b = CountingBridge(dispatch=d_b, redis_factory=lambda: redis)
    return a, b, d_a, d_b


def _frame(rec: str, seq: int, label: str = "1") -> SiprecAudioFrame:
    return SiprecAudioFrame(
        recording_session_id=rec,
        label=label,
        sequence=seq,
        audio_format=AudioFormat.MULAW_8K,
        payload=b"\xff" * 160,
    )


async def _start(bridge: SiprecBridge, rec: str):
    return await bridge.handle_started(
        recording_session_id=rec,
        tenant_id=TENANT,
        provider="siprec",
        agent_user_id=AGENT,
        src_call_id="call-1",
        src_metadata={},
        is_consent_attested=True,
    )


# ── Cross-worker frame routing ─────────────────────────────────────────


def test_frames_on_other_worker_are_forwarded_not_dropped():
    async def scenario():
        redis = FakeRedis()
        a, b, d_a, d_b = _two_workers(redis)

        await _start(a, "rec-1")
        # Frame lands on worker B, which never saw recording.started.
        assert await b.handle_audio(_frame("rec-1", 1)) is True
        assert len(d_b.frames) == 1
        # Worker B lazily opened its own dispatch session.
        assert d_b.opened == ["rec-1"]

    asyncio.run(scenario())


def test_sequence_guard_holds_across_workers():
    async def scenario():
        redis = FakeRedis()
        a, b, _, d_b = _two_workers(redis)
        await _start(a, "rec-1")

        assert await a.handle_audio(_frame("rec-1", 5)) is True
        # Same sequence replayed to the other worker → duplicate.
        assert await b.handle_audio(_frame("rec-1", 5)) is False
        # Older frame → dropped too.
        assert await b.handle_audio(_frame("rec-1", 4)) is False
        # Newer frame → accepted.
        assert await b.handle_audio(_frame("rec-1", 6)) is True
        # Labels are independent streams.
        assert await b.handle_audio(_frame("rec-1", 1, label="2")) is True

    asyncio.run(scenario())


# ── Started idempotency / claim ────────────────────────────────────────


def test_concurrent_started_across_workers_inserts_once():
    async def scenario():
        redis = FakeRedis()
        a, b, d_a, d_b = _two_workers(redis)

        state_a, state_b = await asyncio.gather(_start(a, "rec-1"), _start(b, "rec-1"))
        assert a.persist_started_calls + b.persist_started_calls == 1
        assert state_a.live_session_id == state_b.live_session_id

    asyncio.run(scenario())


def test_started_retry_on_other_worker_reuses_session():
    async def scenario():
        redis = FakeRedis()
        a, b, _, _ = _two_workers(redis)
        first = await _start(a, "rec-1")
        second = await _start(b, "rec-1")  # SRS retry hits worker B
        assert first.live_session_id == second.live_session_id
        assert a.persist_started_calls == 1
        assert b.persist_started_calls == 0

    asyncio.run(scenario())


def test_concurrent_started_same_worker_inserts_once_in_memory():
    """The old check-then-act race: two concurrent started events in
    one process must insert exactly one session (no Redis involved)."""

    async def scenario():
        d = RecordingDispatch()
        bridge = CountingBridge(dispatch=d)

        s1, s2 = await asyncio.gather(_start(bridge, "rec-1"), _start(bridge, "rec-1"))
        assert bridge.persist_started_calls == 1
        assert s1.live_session_id == s2.live_session_id
        assert d.opened == ["rec-1"]

    asyncio.run(scenario())


# ── Stop + reaper ──────────────────────────────────────────────────────


def test_stopped_on_other_worker_tears_down_shared_state():
    async def scenario():
        redis = FakeRedis()
        a, b, d_a, d_b = _two_workers(redis)
        # Force worker A to revalidate its cache on every frame so the
        # test doesn't wait out the 30s revalidation window.
        a._CACHE_REVALIDATE_SECONDS = 0.0

        await _start(a, "rec-1")
        assert await b.handle_audio(_frame("rec-1", 1)) is True

        await b.handle_stopped(recording_session_id="rec-1", reason="normal")
        assert ("rec-1", "normal") in d_b.closed
        assert redis.store.get("siprec:sess:rec-1") is None

        # Worker A revalidates against Redis, notices the session is
        # gone, evicts its cache, and drops the frame.
        assert await a.handle_audio(_frame("rec-1", 2)) is False
        assert a.get_state("rec-1") is None

    asyncio.run(scenario())


def test_reaper_local_sweep_closes_stale_dispatch():
    async def scenario():
        redis = FakeRedis()
        a, b, d_a, d_b = _two_workers(redis)
        await _start(a, "rec-1")
        assert await b.handle_audio(_frame("rec-1", 1)) is True  # B lazily opened

        # Session expires silently (SRS died, no recording.stopped).
        redis.expire_key("siprec:sess:rec-1")
        redis.expire_key("siprec:seq:rec-1")

        await a.reap_stale_sessions()
        await b.reap_stale_sessions()
        assert ("rec-1", "idle_timeout") in d_a.closed
        assert ("rec-1", "idle_timeout") in d_b.closed
        assert a.get_state("rec-1") is None
        assert b.get_state("rec-1") is None

    asyncio.run(scenario())


def test_idle_expiry_drops_frames_for_unknown_session():
    async def scenario():
        redis = FakeRedis()
        a, b, _, d_b = _two_workers(redis)
        await _start(a, "rec-1")

        # Simulate TTL expiry (SRS died silently) — worker B has no
        # local cache, so its frames must be dropped.
        redis.expire_key("siprec:sess:rec-1")
        assert await b.handle_audio(_frame("rec-1", 1)) is False
        assert d_b.frames == []

    asyncio.run(scenario())
