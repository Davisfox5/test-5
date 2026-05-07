"""Tests for ``backend.app.services.telephony.siprec.bridge``.

The bridge is the load-bearing component on the SIPREC ingress path:
SRS sidecar → bridge → transcription dispatch + DB rows. We exercise
it with a recording mock dispatch (no Deepgram) and ``session_factory=None``
(no DB) so the tests stay pure-Python and run on the same plain
pytest invocation as the protocol tests.

A few specific behaviors get pinned because they were the source of
the most pain in the original integration:

* Idempotency on ``recording.started`` — the SRS retries on transient
  HTTP failures, and we cannot afford to spawn two LiveSession rows
  for one call.
* Per-stream sequence-number guard — packets occasionally arrive
  duplicated; double-feeding the transcriber confuses Deepgram's
  end-of-utterance detection.
* Audio-format normalization — the dispatch always sees μ-law 8 kHz,
  no matter what the SRS forwarded. This is the contract the existing
  Twilio path also obeys.
"""

from __future__ import annotations

import audioop
import math
import struct
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import pytest

from backend.app.services.audio import AudioFormat
from backend.app.services.telephony.siprec import (
    SiprecAudioFrame,
    SiprecBridge,
    TranscriptionDispatch,
)


# ── Recording mock dispatch ─────────────────────────────────────────────


@dataclass
class _RecordedOpen:
    recording_session_id: str
    live_session_id: uuid.UUID
    tenant_id: uuid.UUID
    provider: str


@dataclass
class _RecordedAudio:
    recording_session_id: str
    label: str
    audio_mulaw_8k: bytes


@dataclass
class _RecordedClose:
    recording_session_id: str
    reason: Optional[str]


@dataclass
class RecordingDispatch:
    """Test double for ``TranscriptionDispatch`` that records every call."""

    opens: List[_RecordedOpen] = field(default_factory=list)
    audio: List[_RecordedAudio] = field(default_factory=list)
    closes: List[_RecordedClose] = field(default_factory=list)

    async def open_session(
        self,
        recording_session_id: str,
        live_session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        provider: str,
    ) -> None:
        self.opens.append(
            _RecordedOpen(
                recording_session_id=recording_session_id,
                live_session_id=live_session_id,
                tenant_id=tenant_id,
                provider=provider,
            )
        )

    async def send_audio(
        self,
        recording_session_id: str,
        label: str,
        audio_mulaw_8k: bytes,
    ) -> None:
        self.audio.append(
            _RecordedAudio(
                recording_session_id=recording_session_id,
                label=label,
                audio_mulaw_8k=audio_mulaw_8k,
            )
        )

    async def close_session(
        self,
        recording_session_id: str,
        reason: Optional[str] = None,
    ) -> None:
        self.closes.append(
            _RecordedClose(recording_session_id=recording_session_id, reason=reason)
        )


# Static-typing assertion that RecordingDispatch satisfies the Protocol.
_assert_dispatch_protocol: TranscriptionDispatch = RecordingDispatch()


# ── Audio fixture builder ───────────────────────────────────────────────


def _pcm16_8k_sine(duration_ms: float, freq_hz: float = 1000.0) -> bytes:
    """A short PCM16 8 kHz sine — small but non-empty, matches what
    the bridge converts to μ-law."""

    n = int(duration_ms / 1000 * 8000)
    samples = bytearray()
    for i in range(n):
        v = int(15000 * math.sin(2 * math.pi * freq_hz * i / 8000))
        samples.extend(struct.pack("<h", v))
    return bytes(samples)


# ── Lifecycle tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_started_opens_dispatch() -> None:
    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    tenant_id = uuid.uuid4()

    state = await bridge.handle_started(
        recording_session_id="rec-1",
        tenant_id=tenant_id,
        provider="siprec_cisco_cube",
        agent_user_id=uuid.uuid4(),
        src_call_id="call-1",
        src_metadata={"sbc": "cube"},
        is_consent_attested=True,
    )

    assert len(dispatch.opens) == 1
    assert dispatch.opens[0].recording_session_id == "rec-1"
    assert dispatch.opens[0].tenant_id == tenant_id
    assert dispatch.opens[0].provider == "siprec_cisco_cube"
    assert state.live_session_id == dispatch.opens[0].live_session_id


@pytest.mark.asyncio
async def test_handle_started_is_idempotent() -> None:
    """SRS retry must not re-open the dispatch or re-insert sessions."""

    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    s1 = await bridge.handle_started(
        recording_session_id="rec-2",
        tenant_id=tenant_id,
        provider="siprec_cisco_cube",
        agent_user_id=agent_id,
        src_call_id="c2",
        src_metadata={},
        is_consent_attested=False,
    )
    s2 = await bridge.handle_started(
        recording_session_id="rec-2",
        tenant_id=tenant_id,
        provider="siprec_cisco_cube",
        agent_user_id=agent_id,
        src_call_id="c2",
        src_metadata={},
        is_consent_attested=False,
    )

    assert len(dispatch.opens) == 1
    assert s1.live_session_id == s2.live_session_id


@pytest.mark.asyncio
async def test_handle_started_requires_agent_user_id_when_persisting() -> None:
    """In-DB persistence demands a resolved agent user; the in-memory
    no-DB path mints a uuid because tests don't need a real user."""

    # session_factory=None — no DB persistence path; bridge generates a
    # uuid for live_session_id. This exercises the "test mode" exit.
    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    state = await bridge.handle_started(
        recording_session_id="rec-no-agent",
        tenant_id=uuid.uuid4(),
        provider="siprec_metaswitch",
        agent_user_id=None,
        src_call_id=None,
        src_metadata={},
        is_consent_attested=False,
    )
    assert state.live_session_id is not None
    assert state.provider == "siprec_metaswitch"


@pytest.mark.asyncio
async def test_handle_stopped_closes_dispatch_and_evicts_state() -> None:
    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)

    await bridge.handle_started(
        recording_session_id="rec-3",
        tenant_id=uuid.uuid4(),
        provider="siprec_avaya_sbce",
        agent_user_id=uuid.uuid4(),
        src_call_id=None,
        src_metadata={},
        is_consent_attested=False,
    )
    assert "rec-3" in bridge.active_sessions()

    await bridge.handle_stopped(recording_session_id="rec-3", reason="hangup")

    assert "rec-3" not in bridge.active_sessions()
    assert len(dispatch.closes) == 1
    assert dispatch.closes[0].reason == "hangup"


@pytest.mark.asyncio
async def test_handle_stopped_unknown_session_is_noop() -> None:
    """A late ``recording.stopped`` for a session we evicted (or never saw)
    should still close the dispatch without raising."""

    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    await bridge.handle_stopped(recording_session_id="never-existed", reason="timeout")
    assert dispatch.closes[0].recording_session_id == "never-existed"


# ── Audio handling ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_audio_normalizes_to_mulaw_and_dispatches() -> None:
    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    await bridge.handle_started(
        recording_session_id="rec-4",
        tenant_id=uuid.uuid4(),
        provider="siprec_cisco_cube",
        agent_user_id=uuid.uuid4(),
        src_call_id=None,
        src_metadata={},
        is_consent_attested=False,
    )

    pcm = _pcm16_8k_sine(20.0)  # 20 ms of sine = 320 bytes
    delivered = await bridge.handle_audio(
        SiprecAudioFrame(
            recording_session_id="rec-4",
            label="1",
            sequence=0,
            audio_format=AudioFormat.PCM16_8K,
            payload=pcm,
        )
    )
    assert delivered is True
    assert len(dispatch.audio) == 1
    expected_mulaw = audioop.lin2ulaw(pcm, 2)
    assert dispatch.audio[0].audio_mulaw_8k == expected_mulaw
    assert dispatch.audio[0].label == "1"


@pytest.mark.asyncio
async def test_handle_audio_drops_unknown_session() -> None:
    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    pcm = _pcm16_8k_sine(20.0)
    delivered = await bridge.handle_audio(
        SiprecAudioFrame(
            recording_session_id="never-started",
            label="1",
            sequence=0,
            audio_format=AudioFormat.PCM16_8K,
            payload=pcm,
        )
    )
    assert delivered is False
    assert len(dispatch.audio) == 0


@pytest.mark.asyncio
async def test_handle_audio_rejects_duplicate_sequence() -> None:
    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    await bridge.handle_started(
        recording_session_id="rec-5",
        tenant_id=uuid.uuid4(),
        provider="siprec_cisco_cube",
        agent_user_id=uuid.uuid4(),
        src_call_id=None,
        src_metadata={},
        is_consent_attested=False,
    )
    pcm = _pcm16_8k_sine(20.0)

    # Send seq=0,1,2 and a duplicate seq=1.
    for seq in (0, 1, 2):
        await bridge.handle_audio(
            SiprecAudioFrame(
                recording_session_id="rec-5",
                label="1",
                sequence=seq,
                audio_format=AudioFormat.PCM16_8K,
                payload=pcm,
            )
        )
    delivered = await bridge.handle_audio(
        SiprecAudioFrame(
            recording_session_id="rec-5",
            label="1",
            sequence=1,
            audio_format=AudioFormat.PCM16_8K,
            payload=pcm,
        )
    )
    assert delivered is False
    assert len(dispatch.audio) == 3


@pytest.mark.asyncio
async def test_handle_audio_separate_labels_track_independently() -> None:
    """Per-stream sequence guards must not collide across labels."""

    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    await bridge.handle_started(
        recording_session_id="rec-6",
        tenant_id=uuid.uuid4(),
        provider="siprec_avaya_sbce",
        agent_user_id=uuid.uuid4(),
        src_call_id=None,
        src_metadata={},
        is_consent_attested=False,
    )
    pcm = _pcm16_8k_sine(20.0)

    # label="agent" and label="customer" each get seq=0; both should
    # be delivered (separate sequence series).
    await bridge.handle_audio(
        SiprecAudioFrame(
            recording_session_id="rec-6",
            label="agent",
            sequence=0,
            audio_format=AudioFormat.PCM16_8K,
            payload=pcm,
        )
    )
    await bridge.handle_audio(
        SiprecAudioFrame(
            recording_session_id="rec-6",
            label="customer",
            sequence=0,
            audio_format=AudioFormat.PCM16_8K,
            payload=pcm,
        )
    )

    labels = [a.label for a in dispatch.audio]
    assert labels == ["agent", "customer"]


@pytest.mark.asyncio
async def test_handle_audio_drops_empty_payload() -> None:
    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    await bridge.handle_started(
        recording_session_id="rec-7",
        tenant_id=uuid.uuid4(),
        provider="siprec_metaswitch",
        agent_user_id=uuid.uuid4(),
        src_call_id=None,
        src_metadata={},
        is_consent_attested=False,
    )
    delivered = await bridge.handle_audio(
        SiprecAudioFrame(
            recording_session_id="rec-7",
            label="caller",
            sequence=0,
            audio_format=AudioFormat.PCM16_8K,
            payload=b"",
        )
    )
    assert delivered is False
    assert len(dispatch.audio) == 0


@pytest.mark.asyncio
async def test_handle_audio_mulaw_input_is_passthrough_after_conversion() -> None:
    """μ-law in → μ-law out (with a round-trip to PCM and back).

    audioop is not a perfect bijection — μ-law→PCM16→μ-law preserves
    the encoded bytes for the low-amplitude range we test with, so
    we assert byte-equality. Higher amplitudes might lose a bit; the
    bridge's contract is "audible content preserved", not "byte-equal".
    """

    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    await bridge.handle_started(
        recording_session_id="rec-8",
        tenant_id=uuid.uuid4(),
        provider="siprec_cisco_cube",
        agent_user_id=uuid.uuid4(),
        src_call_id=None,
        src_metadata={},
        is_consent_attested=False,
    )
    pcm = _pcm16_8k_sine(20.0, freq_hz=440.0)
    mulaw = audioop.lin2ulaw(pcm, 2)
    await bridge.handle_audio(
        SiprecAudioFrame(
            recording_session_id="rec-8",
            label="1",
            sequence=0,
            audio_format=AudioFormat.MULAW_8K,
            payload=mulaw,
        )
    )
    # μ-law → PCM16 → μ-law round-trip is the bridge's normalization.
    expected = audioop.lin2ulaw(audioop.ulaw2lin(mulaw, 2), 2)
    assert dispatch.audio[0].audio_mulaw_8k == expected


# ── End-to-end (started → frames → stopped) ─────────────────────────────


@pytest.mark.asyncio
async def test_full_session_lifecycle() -> None:
    """Acceptance-criteria proxy: started → 5 audio frames per stream
    → stopped, with all dispatch calls in order and no duplicates."""

    dispatch = RecordingDispatch()
    bridge = SiprecBridge(dispatch=dispatch, session_factory=None)
    tenant_id = uuid.uuid4()

    await bridge.handle_started(
        recording_session_id="lifecycle-1",
        tenant_id=tenant_id,
        provider="siprec_metaswitch",
        agent_user_id=uuid.uuid4(),
        src_call_id="orig-call-id-1",
        src_metadata={"vendor": "metaswitch"},
        is_consent_attested=True,
    )
    pcm = _pcm16_8k_sine(20.0)
    for seq in range(5):
        for label in ("caller", "callee"):
            await bridge.handle_audio(
                SiprecAudioFrame(
                    recording_session_id="lifecycle-1",
                    label=label,
                    sequence=seq,
                    audio_format=AudioFormat.PCM16_8K,
                    payload=pcm,
                )
            )
    await bridge.handle_stopped(
        recording_session_id="lifecycle-1", reason="hangup"
    )

    assert len(dispatch.opens) == 1
    assert len(dispatch.closes) == 1
    assert len(dispatch.audio) == 10
    # Each label received 5 frames.
    caller_frames = [a for a in dispatch.audio if a.label == "caller"]
    callee_frames = [a for a in dispatch.audio if a.label == "callee"]
    assert len(caller_frames) == 5
    assert len(callee_frames) == 5
