"""AudioHook mid-call reconnect position resume (challenge #2c / C3).

Genesys reconnects a broken AudioHook session with a fresh WebSocket
(new session id, same conversationId). The session state machine now
carries a per-conversation audio position through an injected
PositionStore:

* clean client ``close`` → conversation over → position cleared;
* dirty transport drop → position saved for the next connection;
* re-open for the same conversation → position restored, so
  ``audio_position_sec()`` continues instead of restarting at zero;
* probe sessions never touch the store.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, Dict, List, Optional

from backend.app.services.telephony.audiohook.server import (
    AudiohookSession,
    AudiohookSessionState,
)
from tests.fixtures.audiohook.sessions import (
    audio_session_pcmu_with_pause,
    probe_session,
)

TENANT_ID = "55555555-5555-5555-5555-555555555555"


class _FakeTransport:
    def __init__(self, inbound: List[Dict[str, Any]]) -> None:
        self._queue = deque(inbound)
        self.outbound: List[Dict[str, Any]] = []
        self.closed_code: Optional[int] = None

    async def receive(self) -> Dict[str, Any]:
        if self.closed_code is not None or not self._queue:
            return {"type": "websocket.disconnect", "code": 1000}
        item = dict(self._queue.popleft())
        item.setdefault("type", "websocket.receive")
        return item

    async def send_text(self, data: str) -> None:
        self.outbound.append({"text": data})

    async def send_bytes(self, data: bytes) -> None:
        self.outbound.append({"bytes": data})

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


class _NullSink:
    async def on_open(self, state: AudiohookSessionState) -> None:
        pass

    async def on_audio(self, state: AudiohookSessionState, payload: bytes) -> None:
        pass

    async def on_paused(self, state: AudiohookSessionState) -> None:
        pass

    async def on_resumed(self, state: AudiohookSessionState) -> None:
        pass

    async def on_close(self, state: AudiohookSessionState) -> None:
        pass


class _FakeStore:
    def __init__(self, initial: float = 0.0) -> None:
        self.value = initial
        self.loads = 0
        self.saved: List[float] = []
        self.clears = 0

    async def load(self, state: AudiohookSessionState) -> float:
        self.loads += 1
        return self.value

    async def save(self, state: AudiohookSessionState) -> None:
        self.saved.append(state.audio_position_sec())

    async def clear(self, state: AudiohookSessionState) -> None:
        self.clears += 1


def _without_close(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip the client ``close`` and everything after it, simulating a
    dirty transport drop mid-conversation."""
    out: List[Dict[str, Any]] = []
    for f in frames:
        if "text" in f:
            try:
                if json.loads(f["text"]).get("type") == "close":
                    break
            except Exception:
                pass
        out.append(f)
    return out


def _run(frames: List[Dict[str, Any]], store: _FakeStore) -> AudiohookSession:
    transport = _FakeTransport(frames)
    session = AudiohookSession(
        transport=transport,
        sink=_NullSink(),
        tenant_id=TENANT_ID,
        position_store=store,
    )
    asyncio.run(session.handle())
    return session


def test_clean_close_clears_position():
    store = _FakeStore()
    session = _run(audio_session_pcmu_with_pause(), store)
    assert store.loads == 1
    assert store.clears == 1
    assert store.saved == []
    assert session.state.resume_offset_sec == 0.0


def test_dirty_disconnect_saves_position():
    store = _FakeStore()
    session = _run(_without_close(audio_session_pcmu_with_pause()), store)
    assert store.clears == 0
    assert len(store.saved) == 1
    # PCMU 8 kHz two channels → 16000 bytes/s. The fixture delivers two
    # 64-byte frames outside the paused window (the paused frame is
    # dropped before counting).
    expected = session.state.audio_bytes_received / 16000.0
    assert abs(store.saved[0] - expected) < 1e-9
    assert store.saved[0] > 0.0


def test_reopen_restores_position_and_timeline_continues():
    store = _FakeStore(initial=12.5)
    session = _run(_without_close(audio_session_pcmu_with_pause()), store)
    assert session.state.resume_offset_sec == 12.5
    # Position = resume offset + this connection's audio.
    assert session.state.audio_position_sec() > 12.5
    # The dirty drop persisted the cumulative position, not just this
    # connection's slice.
    assert abs(
        store.saved[0]
        - (12.5 + session.state.audio_bytes_received / 16000.0)
    ) < 1e-9


def test_probe_session_never_touches_store():
    store = _FakeStore()
    _run(probe_session(), store)
    assert store.loads == 0
    assert store.saved == []
    assert store.clears == 0
