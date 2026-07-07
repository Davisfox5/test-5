"""Tests for the SIPREC Deepgram transcription dispatch.

This is the component that closes the SIPREC last-mile gap: the bridge
used to run on ``_NullDispatch`` (audio discarded), so a deployed SRS
produced session rows but never a transcript. ``DeepgramSiprecDispatch``
opens a Deepgram live connection per SIP stream, appends final results to
the shared live-transcript Redis buffer, and finalises through the same
``_dispatch_batch_analysis`` path every other channel uses.

We exercise it with a fake Deepgram connection (the SDK isn't installed
in tests, so the handler registers under the ``"Results"`` wire name), a
fake Redis, and an injected finalizer — no network, no Celery.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from backend.app.services.telephony.siprec.dispatch import DeepgramSiprecDispatch


# ── Fakes ────────────────────────────────────────────────────────────


class _FakePipe:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis

    def rpush(self, key: str, val: str):
        self._redis.lists.setdefault(key, []).append(val)
        return self

    def expire(self, key: str, ttl: int):
        self._redis.expires[key] = ttl
        return self

    async def execute(self):
        return None


class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict = {}
        self.expires: dict = {}

    def pipeline(self, transaction: bool = False):
        return _FakePipe(self)


class _FakeConn:
    def __init__(self) -> None:
        self.handlers: dict = {}
        self.started = None
        self.sent: list = []
        self.finished = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def start(self, opts):
        self.started = opts

    async def send(self, audio: bytes):
        self.sent.append(audio)

    async def finish(self):
        self.finished = True

    def emit_final(self, text: str) -> None:
        self.handlers["Results"](
            self,
            {
                "is_final": True,
                "channel": {"alternatives": [{"transcript": text}]},
            },
        )

    def emit_interim(self, text: str) -> None:
        self.handlers["Results"](
            self,
            {
                "is_final": False,
                "channel": {"alternatives": [{"transcript": text}]},
            },
        )


@pytest.mark.asyncio
async def test_dispatch_streams_finals_and_finalizes():
    created: list = []

    def factory():
        conn = _FakeConn()
        created.append(conn)
        return conn

    finalized: list = []

    async def fake_finalizer(redis, session_id):
        finalized.append((redis, session_id))

    redis = _FakeRedis()
    dispatch = DeepgramSiprecDispatch(
        connection_factory=factory,
        redis_client=redis,
        finalizer=fake_finalizer,
    )

    live_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    await dispatch.open_session("rec1", live_id, tenant_id, "cisco_cube")

    # First frame on label "1" lazily opens a Deepgram connection.
    await dispatch.send_audio("rec1", "1", b"\xff" * 160)
    assert len(created) == 1
    conn = created[0]
    assert conn.started["encoding"] == "mulaw"
    assert conn.started["sample_rate"] == 8000
    assert conn.sent == [b"\xff" * 160]

    # Interim results are ignored; only finals hit the buffer.
    conn.emit_interim("hel")
    conn.emit_final("hello there")
    await asyncio.sleep(0.05)  # let run_coroutine_threadsafe drain

    buf_key = f"live:{live_id}:buffer"
    assert buf_key in redis.lists
    seg = json.loads(redis.lists[buf_key][0])
    assert seg["text"] == "hello there"
    assert seg["speaker"] == "1"  # SIP label → speaker attribution
    assert isinstance(seg["timestamp"], (int, float))
    assert redis.expires[buf_key] > 0  # TTL safety net set

    # A second participant stream opens its own connection with its label.
    await dispatch.send_audio("rec1", "2", b"\x00" * 160)
    assert len(created) == 2
    created[1].emit_final("hi back")
    await asyncio.sleep(0.05)
    speakers = {json.loads(s)["speaker"] for s in redis.lists[buf_key]}
    assert speakers == {"1", "2"}

    # Close finishes every connection and finalises by LiveSession id.
    await dispatch.close_session("rec1", reason="stopped")
    assert conn.finished and created[1].finished
    assert finalized == [(redis, str(live_id))]


@pytest.mark.asyncio
async def test_dispatch_without_factory_is_noop():
    """No Deepgram SDK / API key → open/send/close never raise and drop."""
    finalized: list = []

    async def fake_finalizer(redis, session_id):
        finalized.append(session_id)

    dispatch = DeepgramSiprecDispatch(
        api_key="",  # no key + no explicit factory → drop-everything path
        connection_factory=None,
        redis_client=_FakeRedis(),
        finalizer=fake_finalizer,
    )
    live_id = uuid.uuid4()
    await dispatch.open_session("rec1", live_id, uuid.uuid4(), "avaya_sbce")
    await dispatch.send_audio("rec1", "1", b"\xff" * 160)  # dropped, no crash
    await dispatch.close_session("rec1")
    # Session was never registered, so no finalize fires.
    assert finalized == []
