"""Tests for the Redis-backed Telnyx call_control_id → LiveSession map.

The map lets ``call.hangup`` find the right session even when a tenant
has overlapping calls. Previously we fell back to "most recent active
session", which broke for busy tenants. These tests verify:

* ``remember`` writes the key with TTL.
* ``lookup`` round-trips to the same UUID.
* ``forget`` deletes the key.
* Redis being down is non-fatal (lookup returns None, remember/forget
  swallow the error).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


class FakeRedis:
    """In-memory async Redis mock covering the three operations the
    session map uses (set with ex, get, delete, aclose)."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.closed = False

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        return 1

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch ``redis.asyncio.from_url`` so the session-map helpers talk
    to our in-memory fake."""
    fake = FakeRedis()

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", lambda *a, **kw: fake)

    # Also make sure get_settings().REDIS_URL doesn't blow up.
    from backend.app.api import telephony as tele

    monkeypatch.setattr(
        tele,
        "get_settings",
        lambda: SimpleNamespace(REDIS_URL="redis://fake/0"),
    )
    return fake


@pytest.mark.asyncio
async def test_remember_writes_key_with_ttl(fake_redis):
    from backend.app.api.telephony import (
        _TELNYX_SESSION_KEY_PREFIX,
        _TELNYX_SESSION_TTL_SECONDS,
        _telnyx_remember_session,
    )

    session_id = uuid.uuid4()
    await _telnyx_remember_session("cc-abc", session_id)

    key = f"{_TELNYX_SESSION_KEY_PREFIX}:cc-abc"
    assert fake_redis.store[key] == str(session_id)
    # 12-hour TTL — generous enough for long calls, short enough that
    # stale pins age out.
    assert fake_redis.ttls[key] == _TELNYX_SESSION_TTL_SECONDS
    # Connection must be closed after each helper call to avoid leaks.
    assert fake_redis.closed is True


@pytest.mark.asyncio
async def test_lookup_round_trip(fake_redis):
    from backend.app.api.telephony import (
        _telnyx_lookup_session,
        _telnyx_remember_session,
    )

    session_id = uuid.uuid4()
    await _telnyx_remember_session("cc-xyz", session_id)

    got = await _telnyx_lookup_session("cc-xyz")
    assert got == session_id


@pytest.mark.asyncio
async def test_lookup_missing_returns_none(fake_redis):
    from backend.app.api.telephony import _telnyx_lookup_session

    assert await _telnyx_lookup_session("never-set") is None


@pytest.mark.asyncio
async def test_lookup_invalid_uuid_returns_none(fake_redis):
    """If something corrupt landed in Redis (e.g. older format), the
    lookup should degrade gracefully to None rather than crashing the
    webhook handler."""
    from backend.app.api.telephony import (
        _TELNYX_SESSION_KEY_PREFIX,
        _telnyx_lookup_session,
    )

    fake_redis.store[f"{_TELNYX_SESSION_KEY_PREFIX}:cc-bad"] = "not-a-uuid"

    assert await _telnyx_lookup_session("cc-bad") is None


@pytest.mark.asyncio
async def test_forget_deletes_key(fake_redis):
    from backend.app.api.telephony import (
        _TELNYX_SESSION_KEY_PREFIX,
        _telnyx_forget_session,
        _telnyx_remember_session,
    )

    session_id = uuid.uuid4()
    await _telnyx_remember_session("cc-gone", session_id)
    key = f"{_TELNYX_SESSION_KEY_PREFIX}:cc-gone"
    assert key in fake_redis.store

    await _telnyx_forget_session("cc-gone")
    assert key not in fake_redis.store


@pytest.mark.asyncio
async def test_redis_down_is_non_fatal(monkeypatch):
    """If Redis can't be reached, the helpers must not raise — the
    webhook handler falls back to "most recent active session" and
    still services the call."""
    import redis.asyncio as aioredis
    from backend.app.api import telephony as tele

    def _boom(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(aioredis, "from_url", _boom)
    monkeypatch.setattr(
        tele,
        "get_settings",
        lambda: SimpleNamespace(REDIS_URL="redis://fake/0"),
    )

    # None of these should raise.
    await tele._telnyx_remember_session("cc-x", uuid.uuid4())
    assert await tele._telnyx_lookup_session("cc-x") is None
    await tele._telnyx_forget_session("cc-x")
