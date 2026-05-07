"""AudioHook Protocol message-types, parsers, and serializers.

The AudioHook Protocol defines two frame kinds over a single
WebSocket:

* **Text frames** — JSON control messages with a ``version``,
  ``type``, ``seq``, ``clientseq``, ``id`` (session id) and ``parameters``
  payload. The ``type`` field drives the state machine.
* **Binary frames** — raw audio bytes in the format negotiated by the
  ``open`` / ``opened`` exchange. There is no per-frame header; sample
  position is tracked by accumulating frame byte-counts against the
  negotiated rate / channel count.

The full spec lives at https://developer.genesys.cloud/devapps/audiohook/.
This module only models the subset Stream 4 needs for inbound media
ingestion (probe, open/opened, ping/pong, pause/resume, close/closed).
We do NOT generate ``error``/``reconnect``/``disconnect`` messages
proactively; the server module emits them as protocol-violation
responses.

Format mapping:

==================  ==========================================
AudioHook ``format``  ``services.audio.AudioFormat``
==================  ==========================================
``PCMU``              ``MULAW_8K`` (G.711 μ-law, 8 kHz)
``L16`` (rate 8000)   ``PCM16_8K`` (signed 16-bit big-endian)
``L16`` (rate 16000)  ``PCM16_16K`` (signed 16-bit big-endian)
==================  ==========================================

Note on byte order: AudioHook L16 is documented as network-order
(big-endian); :class:`AudioFormat` PCM16 variants are little-endian
internally. :func:`decode_audio_frame` byte-swaps L16 payloads so the
normalizer never sees a big-endian buffer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from backend.app.services.audio.normalizer import AudioFormat


# ── Message types (text-frame ``type`` field) ───────────────────────────


class AudiohookMessageType(str, Enum):
    """AudioHook control-message type values.

    Names mirror the spec exactly so a reader can map straight to the
    Genesys docs. We model only the inbound-media subset; the
    ``agentAssist`` / ``transcript`` extension messages are out of
    scope for Stream 4.
    """

    # Client → server (Genesys Cloud → us):
    OPEN = "open"
    PING = "ping"
    UPDATE = "update"  # mid-session metadata change (rare)
    CLOSE = "close"
    PAUSED = "paused"
    RESUMED = "resumed"
    DISCARDED = "discarded"  # client tells server "discard these seq numbers"
    DTMF = "dtmf"  # DTMF detected mid-call (informational)
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_COMPLETED = "playback_completed"

    # Server → client (us → Genesys Cloud):
    OPENED = "opened"
    PONG = "pong"
    UPDATED = "updated"
    CLOSED = "closed"
    ERROR = "error"
    DISCONNECT = "disconnect"
    PAUSE = "pause"  # server-initiated pause request (PCI flow)
    RESUME = "resume"
    EVENT = "event"  # generic event (e.g. transcript publish)


# Subset of ``open`` connection types we accept. ``connectionProbe``
# is the credential-test connection Genesys fires when an admin saves
# the integration; the spec mandates we accept it without media.
CONNECTION_TYPE_AUDIO = "audio"
CONNECTION_TYPE_PROBE = "connectionProbe"


# ── Negotiated media format ─────────────────────────────────────────────


@dataclass(frozen=True)
class MediaFormat:
    """One ``open.media[]`` entry — a candidate audio format.

    Genesys offers a list of candidates; the server picks one and
    echoes it back in ``opened.media``. ``channels`` mirrors the
    AudioHook channel-name vocabulary: ``"external"`` is the customer
    leg, ``"internal"`` is the agent leg. A two-entry list means
    interleaved stereo; a single entry means mono.
    """

    type: str  # "audio"
    format: str  # "PCMU" | "L16"
    rate: int  # sample rate in Hz
    channels: Tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MediaFormat":
        return cls(
            type=str(raw.get("type", "audio")),
            format=str(raw["format"]),
            rate=int(raw["rate"]),
            channels=tuple(raw.get("channels") or ()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "format": self.format,
            "rate": self.rate,
            "channels": list(self.channels),
        }

    def to_audio_format(self) -> AudioFormat:
        """Map to the normalizer's :class:`AudioFormat` enum.

        Raises ``ValueError`` for combinations the normalizer can't
        consume — this is a programmer error (we should only have
        accepted formats we can decode), surfaced loudly so it's
        caught in tests rather than producing silent garbage audio.
        """

        fmt = self.format.upper()
        if fmt == "PCMU":
            # G.711 μ-law is locked at 8 kHz in AudioHook regardless
            # of the rate field. Genesys won't actually offer PCMU at
            # other rates today; we still pin to MULAW_8K to keep the
            # downstream decoder simple.
            return AudioFormat.MULAW_8K
        if fmt == "L16":
            if self.rate == 8000:
                return AudioFormat.PCM16_8K
            if self.rate == 16000:
                return AudioFormat.PCM16_16K
        raise ValueError(
            f"Unsupported AudioHook media format: format={self.format} rate={self.rate}"
        )


# Server preference order. We pick the first offered format that the
# normalizer can consume. PCMU first because it matches the existing
# Twilio / SignalWire / Telnyx pipeline shape (Deepgram μ-law 8 kHz),
# saving a downsample for live transcription.
_PREFERRED_FORMAT_ORDER: Tuple[Tuple[str, int], ...] = (
    ("PCMU", 8000),
    ("L16", 8000),
    ("L16", 16000),
)


def select_media_format(offered: List[MediaFormat]) -> Optional[MediaFormat]:
    """Pick a server-preferred candidate from the client's offer list.

    Returns ``None`` if no offered format is consumable — the server
    must then send an ``error`` and close. Channel layout is
    inherited from the offer; we don't try to negotiate a different
    channel split.
    """

    if not offered:
        return None
    by_key = {(m.format.upper(), m.rate): m for m in offered}
    for fmt, rate in _PREFERRED_FORMAT_ORDER:
        match = by_key.get((fmt, rate))
        if match is not None:
            return match
    return None


# ── Open / opened message dataclasses ───────────────────────────────────


@dataclass(frozen=True)
class AudiohookOpenMessage:
    """Parsed ``open`` control message.

    Captures the fields Stream 4 actually needs to make routing
    decisions. Fields the spec defines but we don't use today (e.g.
    ``customConfig``, ``language``) are passed through in
    :attr:`raw` so the server module can log them without re-parsing.
    """

    organization_id: str
    conversation_id: str
    participant_id: str
    connection_type: str  # CONNECTION_TYPE_AUDIO | CONNECTION_TYPE_PROBE
    media: List[MediaFormat]
    language: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_parameters(cls, raw: Dict[str, Any]) -> "AudiohookOpenMessage":
        # ``parameters`` is the only place the open payload lives; the
        # surrounding envelope (version, type, seq, id) belongs to the
        # caller.
        return cls(
            organization_id=str(raw.get("organizationId", "")),
            conversation_id=str(raw.get("conversationId", "")),
            participant_id=str((raw.get("participant") or {}).get("id", "")),
            connection_type=str(raw.get("type", CONNECTION_TYPE_AUDIO)),
            media=[MediaFormat.from_dict(m) for m in (raw.get("media") or [])],
            language=raw.get("language"),
            raw=raw,
        )


@dataclass(frozen=True)
class AudiohookOpenedMessage:
    """Server-side ``opened`` payload (the response to ``open``)."""

    media: Optional[MediaFormat]
    start_paused: bool = False

    def to_parameters(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "startPaused": self.start_paused,
            # ``media`` is an array per spec — we pin to one entry
            # because we always pick exactly one candidate.
            "media": [self.media.to_dict()] if self.media else [],
        }
        return params


# ── Text-frame parsing ──────────────────────────────────────────────────


class AudiohookProtocolError(ValueError):
    """Raised when an inbound text frame violates the protocol shape.

    The server module catches this, sends an ``error`` control frame
    back to the client, and closes the WebSocket with code 1002
    (protocol error).
    """


def parse_control_message(raw: bytes | str) -> Dict[str, Any]:
    """Parse and structurally validate an AudioHook text frame.

    Returns the decoded envelope dict. Validation is intentionally
    minimal — we only check the fields the state machine reads. The
    spec versions (``version: "2"`` is current as of this writing)
    are accepted permissively because Genesys may bump it
    independently of the message-type vocabulary.

    Raises :class:`AudiohookProtocolError` for malformed payloads.
    """

    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AudiohookProtocolError(
                "AudioHook text frame is not valid UTF-8"
            ) from exc
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AudiohookProtocolError(
            f"AudioHook text frame is not valid JSON: {exc}"
        ) from exc
    if not isinstance(msg, dict):
        raise AudiohookProtocolError("AudioHook text frame must be a JSON object")
    msg_type = msg.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        raise AudiohookProtocolError("AudioHook message missing string ``type``")
    seq = msg.get("seq")
    if not isinstance(seq, int):
        raise AudiohookProtocolError("AudioHook message ``seq`` must be an int")
    session_id = msg.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise AudiohookProtocolError("AudioHook message missing string ``id``")
    # ``parameters`` is required for every message type we model except
    # ``ping``/``pong`` (where it carries an ``rtt`` but is optional).
    params = msg.get("parameters")
    if params is not None and not isinstance(params, dict):
        raise AudiohookProtocolError("AudioHook ``parameters`` must be an object")
    return msg


def encode_control_message(
    *,
    version: str,
    msg_type: AudiohookMessageType | str,
    seq: int,
    client_seq: int,
    session_id: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> str:
    """Serialize a server-side text frame.

    AudioHook envelopes carry a ``serverseq`` (our sequence) and
    ``clientseq`` (the highest client ``seq`` we've acknowledged) so
    the client can detect drops. The state machine increments
    ``serverseq`` for every text frame we send.
    """

    type_value = (
        msg_type.value if isinstance(msg_type, AudiohookMessageType) else str(msg_type)
    )
    envelope: Dict[str, Any] = {
        "version": version,
        "id": session_id,
        "type": type_value,
        "seq": seq,
        "clientseq": client_seq,
        "parameters": parameters or {},
    }
    return json.dumps(envelope, separators=(",", ":"))


# ── Binary-frame decoding ───────────────────────────────────────────────


def decode_audio_frame(payload: bytes, media_format: MediaFormat) -> bytes:
    """Convert a raw AudioHook binary frame into the normalizer-ready buffer.

    AudioHook delivers L16 in big-endian (network) order; the
    :mod:`audio.normalizer` PCM16 variants assume little-endian
    because that's what stdlib :mod:`audioop` operates on. We
    byte-swap L16 here so callers can pass the result directly to
    ``to_mulaw_8k(buf, AudioFormat.PCM16_*)`` without re-checking
    endianness.

    PCMU is byte-stream natively (single-octet samples), so we pass
    it through unchanged.

    Empty payloads return ``b""`` rather than raising — Genesys
    occasionally sends keep-alive zero-byte audio frames during
    pause windows, and the server should tolerate them silently.
    """

    if not payload:
        return b""
    fmt = media_format.format.upper()
    if fmt == "PCMU":
        return bytes(payload)
    if fmt == "L16":
        if len(payload) % 2 != 0:
            raise AudiohookProtocolError(
                f"L16 audio frame length {len(payload)} is not even"
            )
        # In-place swap from big-endian samples to little-endian.
        ba = bytearray(payload)
        ba[0::2], ba[1::2] = ba[1::2], ba[0::2]
        return bytes(ba)
    raise AudiohookProtocolError(f"Cannot decode AudioHook binary frame: format={fmt}")
