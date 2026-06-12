"""Replay tests for the AudioHook session state machine.

Drives the :class:`AudiohookSession` with an in-memory transport
(no real WebSocket, no DB) and asserts:

* probe → opened → close round-trip;
* full audio session: open → audio → pause/resume → close;
* audio frames received during a paused window are dropped;
* L16 16k payloads are byte-swapped before reaching the sink;
* malformed first message produces an error frame and close;
* persist_open is called on real audio sessions and skipped on
  probes;
* persist_close runs on session end with the tracked frame counts.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any, List, Optional

import pytest

from backend.app.services.telephony.audiohook.server import (
    AudiohookSession,
    AudiohookSessionConfig,
    AudiohookSessionState,
)
from tests.fixtures.audiohook.sessions import (
    SESSION_ID,
    audio_session_l16_customer_only,
    audio_session_pcmu_with_pause,
    malformed_first_message,
    probe_session,
)


TENANT_ID = "55555555-5555-5555-5555-555555555555"


# ── In-memory transport + sink ─────────────────────────────────────────


class _FakeTransport:
    """Buffered WebSocket replacement for state-machine tests.

    ``inbound`` is the script of frames the harness queues for the
    server to consume; ``outbound`` accumulates everything the
    server sends back. ``closed_code`` is ``None`` until ``close``
    is called.
    """

    def __init__(self, inbound: List[dict[str, Any]]) -> None:
        # Each inbound entry mirrors Starlette's receive() shape:
        # {"text": "..."} or {"bytes": b"..."}.
        self._queue: deque[dict[str, Any]] = deque(inbound)
        self.outbound: List[dict[str, Any]] = []
        self.closed_code: Optional[int] = None

    async def receive(self) -> dict[str, Any]:
        if self.closed_code is not None or not self._queue:
            return {"type": "websocket.disconnect", "code": 1000}
        item = self._queue.popleft()
        item = dict(item)
        item.setdefault("type", "websocket.receive")
        return item

    async def send_text(self, data: str) -> None:
        self.outbound.append({"text": data})

    async def send_bytes(self, data: bytes) -> None:
        self.outbound.append({"bytes": data})

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


class _CapturingSink:
    """Records every callback the session invokes for assertions."""

    def __init__(self) -> None:
        self.opened_state: Optional[AudiohookSessionState] = None
        self.audio_chunks: List[bytes] = []
        self.paused_count: int = 0
        self.resumed_count: int = 0
        self.closed_state: Optional[AudiohookSessionState] = None

    async def on_open(self, state: AudiohookSessionState) -> None:
        self.opened_state = state

    async def on_audio(
        self, state: AudiohookSessionState, payload: bytes
    ) -> None:
        self.audio_chunks.append(payload)

    async def on_paused(self, state: AudiohookSessionState) -> None:
        self.paused_count += 1

    async def on_resumed(self, state: AudiohookSessionState) -> None:
        self.resumed_count += 1

    async def on_close(self, state: AudiohookSessionState) -> None:
        self.closed_state = state


def _outbound_text_messages(transport: _FakeTransport) -> List[dict[str, Any]]:
    """Return the JSON-decoded text frames the server sent."""

    return [json.loads(f["text"]) for f in transport.outbound if "text" in f]


# ── Helpers ────────────────────────────────────────────────────────────


async def _run_session(
    inbound: List[dict[str, Any]],
    *,
    persist_open=None,
    persist_close=None,
) -> tuple[_FakeTransport, _CapturingSink, AudiohookSession]:
    transport = _FakeTransport(inbound)
    sink = _CapturingSink()
    session = AudiohookSession(
        transport=transport,
        sink=sink,
        tenant_id=TENANT_ID,
        persist_open=persist_open,
        persist_close=persist_close,
        config=AudiohookSessionConfig(protocol_version="2"),
    )
    await session.handle()
    return transport, sink, session


# ── Probe ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_session_replies_opened_then_closed():
    transport, sink, session = await _run_session(probe_session())
    msgs = _outbound_text_messages(transport)
    assert len(msgs) == 2
    assert msgs[0]["type"] == "opened"
    assert msgs[0]["parameters"]["media"] == []
    assert msgs[0]["id"] == SESSION_ID
    assert msgs[1]["type"] == "closed"
    # No audio session, no sink callbacks beyond on_close.
    assert sink.opened_state is None
    assert sink.audio_chunks == []
    assert transport.closed_code == 1000


@pytest.mark.asyncio
async def test_probe_does_not_persist_open_row():
    persisted: List[Any] = []

    async def persist_open(state):
        persisted.append(state.session_id)
        return "row-1"

    await _run_session(probe_session(), persist_open=persist_open)
    assert persisted == []  # probes never call persist_open


# ── Full audio session with pause/resume ──────────────────────────────


@pytest.mark.asyncio
async def test_audio_session_full_lifecycle():
    persisted_states: List[AudiohookSessionState] = []
    closed_states: List[tuple[AudiohookSessionState, Any]] = []

    async def persist_open(state):
        persisted_states.append(state)
        return "row-7"

    async def persist_close(state, persisted_id):
        closed_states.append((state, persisted_id))

    transport, sink, session = await _run_session(
        audio_session_pcmu_with_pause(),
        persist_open=persist_open,
        persist_close=persist_close,
    )

    msgs = _outbound_text_messages(transport)
    types = [m["type"] for m in msgs]
    # Expected outbound: opened, pong (in response to ping), closed.
    assert types == ["opened", "pong", "closed"]

    # opened.media echoes the PCMU/8000 selection the server made.
    opened_params = msgs[0]["parameters"]
    assert opened_params["media"][0]["format"] == "PCMU"
    assert opened_params["media"][0]["rate"] == 8000
    assert opened_params["media"][0]["channels"] == ["external", "internal"]

    # Two audio frames flow through (pre-pause and post-resume).
    # The frame sent during pause is dropped.
    assert len(sink.audio_chunks) == 2
    assert sink.paused_count == 1
    assert sink.resumed_count == 1

    # Persisted exactly once with the negotiated format.
    assert len(persisted_states) == 1
    state = persisted_states[0]
    assert state.media is not None
    assert state.media.format == "PCMU"
    assert state.channel == "both"

    # persist_close runs once with the row id and tracks the audio counts.
    assert len(closed_states) == 1
    final_state, row_id = closed_states[0]
    assert row_id == "row-7"
    assert final_state.audio_frames_received == 2
    assert final_state.audio_bytes_received > 0


@pytest.mark.asyncio
async def test_audio_session_drops_frames_received_while_paused():
    transport, sink, session = await _run_session(audio_session_pcmu_with_pause())
    # The middle frame in the fixture (16 bytes of 0xAA) must NOT
    # appear in the sink output — it arrived during the paused window.
    leaked_frame = bytes([0xAA] * 16)
    assert leaked_frame not in sink.audio_chunks


# ── L16 byte-swap path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l16_audio_session_byteswaps_payload():
    transport, sink, session = await _run_session(audio_session_l16_customer_only())
    msgs = _outbound_text_messages(transport)
    # opened → closed only; no ping/pong in this fixture.
    assert [m["type"] for m in msgs] == ["opened", "closed"]
    assert msgs[0]["parameters"]["media"][0]["format"] == "L16"
    assert msgs[0]["parameters"]["media"][0]["rate"] == 16000
    # Channel string is "customer" because only ["external"] was offered.
    assert sink.opened_state is not None
    assert sink.opened_state.channel == "customer"
    # The 4-byte big-endian L16 payload should arrive byte-swapped.
    assert sink.audio_chunks == [bytes([0x02, 0x01, 0x04, 0x03])]


# ── Error path: malformed first message ───────────────────────────────


@pytest.mark.asyncio
async def test_first_message_must_be_open():
    transport, sink, session = await _run_session(malformed_first_message())
    msgs = _outbound_text_messages(transport)
    assert len(msgs) == 1
    assert msgs[0]["type"] == "error"
    assert msgs[0]["parameters"]["code"] == "invalid-message"
    # Server closes with protocol-error code.
    assert transport.closed_code == 1002


# ── Error path: open offers no consumable format ──────────────────────


@pytest.mark.asyncio
async def test_open_with_only_unsupported_formats_fails():
    bad = [
        {
            "text": json.dumps(
                {
                    "version": "2",
                    "id": SESSION_ID,
                    "type": "open",
                    "seq": 1,
                    "parameters": {
                        "organizationId": "o",
                        "conversationId": "c",
                        "participant": {"id": "p"},
                        "type": "audio",
                        "media": [
                            {
                                "type": "audio",
                                "format": "OPUS",
                                "rate": 48000,
                                "channels": ["external"],
                            }
                        ],
                    },
                }
            )
        }
    ]
    transport, sink, session = await _run_session(bad)
    msgs = _outbound_text_messages(transport)
    assert msgs[0]["type"] == "error"
    assert msgs[0]["parameters"]["code"] == "unsupported-format"
    assert transport.closed_code == 1002
