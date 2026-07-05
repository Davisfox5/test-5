"""AudioHook session state machine.

One :class:`AudiohookSession` per WebSocket connection. The session
drives the protocol-level state (probe vs. audio, paused/resumed,
seq counters, pong replies) and routes decoded audio into a
:class:`AudioSink`. The transport (FastAPI :class:`WebSocket`) and
the persistence layer (SQLAlchemy session) are injected so the
state machine is unit-testable without a live WebSocket or DB.

Lifecycle
=========

1. Client connects with a signed upgrade. Caller verifies the
   signature via :mod:`.auth` BEFORE handing the socket to this
   class — invalid signatures never reach the state machine.
2. ``handle()`` loops, dispatching text frames to control handlers
   and binary frames to the :class:`AudioSink`. The first text frame
   MUST be ``open``; anything else triggers an ``error`` and close.
3. On ``open`` with type ``connectionProbe``, the server replies
   ``opened`` with an empty media list and waits for ``close``. No
   audio flows. This is the credential-validation path Genesys runs
   when an admin saves an integration.
4. On ``open`` with type ``audio``, the server picks a media format
   from the client offer (:func:`select_media_format`), replies
   ``opened``, and starts accepting binary frames. Pause/resume,
   discarded-frames, ping/pong are all handled inline.
5. On ``close`` (client) the server sends ``closed`` and exits.
   On unrecoverable protocol errors the server sends ``error`` with
   the spec's ``code``/``message`` shape and closes 1002.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

from backend.app.services.telephony.audiohook.protocol import (
    CONNECTION_TYPE_AUDIO,
    CONNECTION_TYPE_PROBE,
    AudiohookMessageType,
    AudiohookOpenedMessage,
    AudiohookOpenMessage,
    AudiohookProtocolError,
    MediaFormat,
    decode_audio_frame,
    encode_control_message,
    parse_control_message,
    select_media_format,
)


logger = logging.getLogger(__name__)


# AudioHook protocol version we advertise. Genesys' current production
# spec is ``"2"``; we echo the version the client sends back so we
# don't break on future minor bumps that don't change the message
# vocabulary.
DEFAULT_PROTOCOL_VERSION = "2"


# ── Transport abstraction ───────────────────────────────────────────────


class AudiohookTransport(Protocol):
    """Minimal WebSocket-like transport the session needs.

    FastAPI's :class:`fastapi.WebSocket` satisfies this Protocol
    structurally. The test suite supplies an in-memory transport so
    the state machine can be exercised without a live socket.
    """

    async def receive(self) -> dict[str, Any]:
        """Return the next ``starlette.types.Message``-shaped dict.

        Standard Starlette/FastAPI shape:

        * ``{"type": "websocket.receive", "text": "..."}`` for text
        * ``{"type": "websocket.receive", "bytes": b"..."}`` for binary
        * ``{"type": "websocket.disconnect", "code": 1000}`` on close
        """

    async def send_text(self, data: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


# ── Audio sink contract ─────────────────────────────────────────────────


class AudioSink(Protocol):
    """Where decoded audio goes.

    Production: a sink that fans audio into Deepgram live + the
    :class:`backend.app.services.paralinguistics_live.LiveParalinguisticWindow`.
    Tests: a list-appending stub.

    The sink owns the source format hint — the session passes raw
    decoded bytes (PCMU passthrough, L16 byte-swapped to LE PCM16)
    plus the negotiated :class:`MediaFormat` so the sink can route
    to the correct transcription path.
    """

    async def on_open(self, session: "AudiohookSessionState") -> None: ...

    async def on_audio(
        self, session: "AudiohookSessionState", payload: bytes
    ) -> None: ...

    async def on_paused(self, session: "AudiohookSessionState") -> None: ...

    async def on_resumed(self, session: "AudiohookSessionState") -> None: ...

    async def on_close(self, session: "AudiohookSessionState") -> None: ...


# ── Persistence callback ────────────────────────────────────────────────


# Returns the persisted row's primary key (UUID). Stored as ``Any`` so
# the server module doesn't take a hard dependency on the DB layer.
PersistOpen = Callable[["AudiohookSessionState"], Awaitable[Any]]
PersistClose = Callable[["AudiohookSessionState", Optional[Any]], Awaitable[None]]


# ── State container ─────────────────────────────────────────────────────


@dataclass
class AudiohookSessionState:
    """Runtime state for one AudioHook session.

    Mutable; the state machine writes to fields here. The
    :class:`AudioSink` and persistence callbacks read this snapshot.
    """

    tenant_id: str
    session_id: str = ""
    organization_id: str = ""
    conversation_id: str = ""
    participant_id: str = ""
    connection_type: str = ""
    media: Optional[MediaFormat] = None
    started_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    ended_at: Optional[datetime] = None
    paused: bool = False
    audio_frames_received: int = 0
    audio_bytes_received: int = 0
    # Seconds of audio already received by a PRIOR connection for the
    # same conversation (mid-call reconnect). Restored from the
    # position store on ``open``; 0.0 for a fresh call.
    resume_offset_sec: float = 0.0
    persisted_id: Optional[Any] = None  # populated by persist_open callback
    server_seq: int = 0
    last_client_seq: int = 0

    # The full ``open`` parameters dict, for downstream consumers
    # (e.g. reading ``customConfig`` to map a Genesys queue id back
    # to an internal team id). Kept opaque on the session.
    open_parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def channel(self) -> str:
        """Short label for the audio channels we negotiated.

        Mirrors :class:`AudiohookSession.channel` enum on the
        persistence layer: ``"agent"`` for ``["internal"]`` only,
        ``"customer"`` for ``["external"]`` only, ``"both"`` for
        both legs (mono mix or stereo). Empty string until the
        ``open`` exchange completes.
        """

        if self.media is None:
            return ""
        chans = {c.lower() for c in self.media.channels}
        if chans == {"internal"}:
            return "agent"
        if chans == {"external"}:
            return "customer"
        if "internal" in chans and "external" in chans:
            return "both"
        return "unknown"

    def audio_position_sec(self) -> float:
        """Total seconds of audio received for this *conversation*,
        including audio streamed by prior connections before a
        mid-call reconnect. Derived from byte counts against the
        negotiated media format (PCMU: 1 byte/sample, L16: 2)."""

        if self.media is None:
            return self.resume_offset_sec
        bytes_per_sample = 2 if self.media.format.upper() == "L16" else 1
        n_channels = max(1, len(self.media.channels))
        bytes_per_sec = float(self.media.rate * bytes_per_sample * n_channels)
        if bytes_per_sec <= 0:
            return self.resume_offset_sec
        return self.resume_offset_sec + self.audio_bytes_received / bytes_per_sec


class PositionStore(Protocol):
    """Durable per-conversation audio position for mid-call reconnects.

    ``load`` is called during the ``open`` exchange of an audio session
    (after ``conversation_id`` and media are known) and returns the
    seconds already streamed by prior connections — 0.0 for a fresh
    conversation. ``save`` is called on a dirty disconnect; ``clear``
    on a clean client ``close`` (conversation over)."""

    async def load(self, state: "AudiohookSessionState") -> float: ...

    async def save(self, state: "AudiohookSessionState") -> None: ...

    async def clear(self, state: "AudiohookSessionState") -> None: ...


# ── The state machine ──────────────────────────────────────────────────


@dataclass
class AudiohookSessionConfig:
    """Knobs for one session run.

    Tests override the version + clock to get deterministic envelopes.
    """

    protocol_version: str = DEFAULT_PROTOCOL_VERSION
    # Optional clock injection — defaults to ``time.time``.
    clock: Callable[[], float] = field(default_factory=lambda: time.time)


class AudiohookSession:
    """One AudioHook connection's state machine."""

    def __init__(
        self,
        *,
        transport: AudiohookTransport,
        sink: AudioSink,
        tenant_id: str,
        persist_open: Optional[PersistOpen] = None,
        persist_close: Optional[PersistClose] = None,
        position_store: Optional[PositionStore] = None,
        config: Optional[AudiohookSessionConfig] = None,
    ) -> None:
        self.transport = transport
        self.sink = sink
        self.persist_open = persist_open
        self.persist_close = persist_close
        self.position_store = position_store
        self.config = config or AudiohookSessionConfig()
        self.state = AudiohookSessionState(tenant_id=tenant_id)
        # Internal flags for the receive loop.
        self._opened = False
        self._closing = False

    # ── Outbound helpers ────────────────────────────────────────────

    def _next_seq(self) -> int:
        self.state.server_seq += 1
        return self.state.server_seq

    async def _send_control(
        self,
        msg_type: AudiohookMessageType,
        parameters: Optional[dict[str, Any]] = None,
    ) -> None:
        text = encode_control_message(
            version=self.config.protocol_version,
            msg_type=msg_type,
            seq=self._next_seq(),
            client_seq=self.state.last_client_seq,
            session_id=self.state.session_id,
            parameters=parameters or {},
        )
        await self.transport.send_text(text)

    async def _send_error(self, code: str, message: str) -> None:
        # The spec uses ``code`` (string enum) + ``message``. Common
        # codes: ``"invalid-message"``, ``"unsupported-format"``,
        # ``"unauthorized"``.
        try:
            await self._send_control(
                AudiohookMessageType.ERROR,
                {"code": code, "message": message},
            )
        except Exception:
            # Best-effort — if the transport is already broken we
            # still try to close cleanly below.
            logger.debug("Failed to send AudioHook error frame", exc_info=True)

    # ── Main loop ──────────────────────────────────────────────────

    async def handle(self) -> None:
        """Drive the session until disconnect.

        Returns when the WebSocket closes, on either side. Does not
        raise — protocol errors are converted into ``error``/close
        frames, transport errors are logged. The caller (the API
        route) wraps the call in its own try/finally for metrics.
        """

        try:
            await self._loop()
        finally:
            self.state.ended_at = datetime.now(tz=timezone.utc)
            # Position bookkeeping for mid-call reconnects: a clean
            # client ``close`` ends the conversation (clear); a dirty
            # transport drop persists the position so the next
            # connection for this conversation resumes the timeline.
            if (
                self.position_store is not None
                and self.state.connection_type == CONNECTION_TYPE_AUDIO
                and self.state.media is not None
            ):
                try:
                    if self._closing:
                        await self.position_store.clear(self.state)
                    else:
                        await self.position_store.save(self.state)
                except Exception:
                    logger.exception("AudioHook position store failed")
            try:
                await self.sink.on_close(self.state)
            except Exception:
                logger.exception("AudioHook sink.on_close failed")
            if self.persist_close is not None:
                try:
                    await self.persist_close(self.state, self.state.persisted_id)
                except Exception:
                    logger.exception("AudioHook persist_close failed")

    async def _loop(self) -> None:
        while True:
            try:
                msg = await self.transport.receive()
            except Exception:
                logger.debug("AudioHook transport receive raised", exc_info=True)
                return
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                return
            if mtype != "websocket.receive":
                # Unknown Starlette frame type — skip rather than
                # treating it as a protocol error.
                continue
            if "text" in msg and msg["text"] is not None:
                if not await self._handle_text(msg["text"]):
                    return
            elif "bytes" in msg and msg["bytes"] is not None:
                await self._handle_binary(msg["bytes"])
            # Else: empty frame, ignored.
            if self._closing:
                return

    # ── Text frame handlers ────────────────────────────────────────

    async def _handle_text(self, raw: str) -> bool:
        """Dispatch a JSON control message. Returns ``False`` to stop the loop."""

        try:
            envelope = parse_control_message(raw)
        except AudiohookProtocolError as exc:
            await self._send_error("invalid-message", str(exc))
            await self.transport.close(code=1002)
            return False

        msg_type = envelope["type"]
        seq = envelope["seq"]
        if isinstance(seq, int):
            self.state.last_client_seq = max(self.state.last_client_seq, seq)
        # On the very first message we MUST see ``open`` (per spec
        # the only other valid first message is the connection
        # close, which we handle below).
        if not self._opened and msg_type not in (
            AudiohookMessageType.OPEN.value,
            AudiohookMessageType.CLOSE.value,
        ):
            await self._send_error(
                "invalid-message",
                f"First message must be 'open', got {msg_type!r}",
            )
            await self.transport.close(code=1002)
            return False

        # Bind the session id from the first ``open`` so subsequent
        # outbound envelopes echo it correctly.
        if not self.state.session_id:
            self.state.session_id = envelope["id"]

        params = envelope.get("parameters") or {}
        if msg_type == AudiohookMessageType.OPEN.value:
            return await self._handle_open(params)
        if msg_type == AudiohookMessageType.PING.value:
            await self._handle_ping(params)
            return True
        if msg_type == AudiohookMessageType.CLOSE.value:
            await self._handle_close(params)
            return False
        if msg_type == AudiohookMessageType.PAUSED.value:
            await self._handle_paused()
            return True
        if msg_type == AudiohookMessageType.RESUMED.value:
            await self._handle_resumed()
            return True
        if msg_type == AudiohookMessageType.DISCARDED.value:
            # Informational — Genesys is telling us to drop frames
            # whose sequence numbers fall in a range. Stream 4 doesn't
            # buffer audio, so there's nothing to discard; we ack via
            # the implicit clientseq bump and move on.
            return True
        if msg_type in (
            AudiohookMessageType.UPDATE.value,
            AudiohookMessageType.DTMF.value,
            AudiohookMessageType.PLAYBACK_STARTED.value,
            AudiohookMessageType.PLAYBACK_COMPLETED.value,
        ):
            # Tolerated but not acted on. ``update`` would let us
            # rebind the conversation/customer mapping mid-call —
            # not in scope for Stream 4.
            return True
        # Unknown message type — log and ignore (forward-compat).
        logger.info("AudioHook: ignoring unknown message type %r", msg_type)
        return True

    async def _handle_open(self, params: dict[str, Any]) -> bool:
        try:
            open_msg = AudiohookOpenMessage.from_parameters(params)
        except Exception as exc:
            await self._send_error("invalid-message", f"Bad open: {exc}")
            await self.transport.close(code=1002)
            return False

        self.state.organization_id = open_msg.organization_id
        self.state.conversation_id = open_msg.conversation_id
        self.state.participant_id = open_msg.participant_id
        self.state.connection_type = open_msg.connection_type
        self.state.open_parameters = open_msg.raw

        if open_msg.connection_type == CONNECTION_TYPE_PROBE:
            # Probe: confirm the session with an empty media list.
            # The client will follow with ``close`` immediately.
            opened = AudiohookOpenedMessage(media=None, start_paused=False)
            await self._send_control(
                AudiohookMessageType.OPENED, opened.to_parameters()
            )
            self._opened = True
            return True

        if open_msg.connection_type != CONNECTION_TYPE_AUDIO:
            await self._send_error(
                "unsupported-connection-type",
                f"Connection type {open_msg.connection_type!r} not supported",
            )
            await self.transport.close(code=1002)
            return False

        chosen = select_media_format(open_msg.media)
        if chosen is None:
            await self._send_error(
                "unsupported-format",
                "No offered media format is consumable by this server",
            )
            await self.transport.close(code=1002)
            return False
        # Verify the chosen format maps to a normalizer-known enum.
        try:
            chosen.to_audio_format()
        except ValueError as exc:
            await self._send_error("unsupported-format", str(exc))
            await self.transport.close(code=1002)
            return False

        self.state.media = chosen

        # Mid-call reconnect: restore how much audio prior connections
        # for this conversation already streamed, so downstream
        # consumers keep a continuous timeline instead of restarting
        # at zero.
        if self.position_store is not None:
            try:
                self.state.resume_offset_sec = float(
                    await self.position_store.load(self.state) or 0.0
                )
                if self.state.resume_offset_sec > 0.0:
                    logger.info(
                        "AudioHook conversation %s re-attached — resuming "
                        "position at %.1fs",
                        self.state.conversation_id,
                        self.state.resume_offset_sec,
                    )
            except Exception:
                logger.exception("AudioHook position restore failed")

        opened = AudiohookOpenedMessage(media=chosen, start_paused=False)
        await self._send_control(AudiohookMessageType.OPENED, opened.to_parameters())
        self._opened = True

        # Persist + notify sink only for real audio sessions, not
        # probes. The probe path gets neither a DB row nor a sink call
        # because there's no media to attach to.
        if self.persist_open is not None:
            try:
                self.state.persisted_id = await self.persist_open(self.state)
            except Exception:
                logger.exception("AudioHook persist_open failed")
        try:
            await self.sink.on_open(self.state)
        except Exception:
            logger.exception("AudioHook sink.on_open failed")
        return True

    async def _handle_ping(self, params: dict[str, Any]) -> None:
        # Echo the ``rtt`` value if present — Genesys uses it to
        # measure server latency. ``params`` may also carry a
        # ``rxtimestamp``; we forward it untouched.
        await self._send_control(AudiohookMessageType.PONG, params)

    async def _handle_close(self, params: dict[str, Any]) -> None:
        # Acknowledge with ``closed`` and let the loop exit. We do
        # NOT attempt to send ``disconnect`` because that's the
        # server-initiated equivalent — the client already chose to
        # end.
        await self._send_control(AudiohookMessageType.CLOSED, {})
        self._closing = True
        try:
            await self.transport.close(code=1000)
        except Exception:
            logger.debug("AudioHook close on transport failed", exc_info=True)

    async def _handle_paused(self) -> None:
        if self.state.paused:
            return
        self.state.paused = True
        try:
            await self.sink.on_paused(self.state)
        except Exception:
            logger.exception("AudioHook sink.on_paused failed")

    async def _handle_resumed(self) -> None:
        if not self.state.paused:
            return
        self.state.paused = False
        try:
            await self.sink.on_resumed(self.state)
        except Exception:
            logger.exception("AudioHook sink.on_resumed failed")

    # ── Binary frame handler ───────────────────────────────────────

    async def _handle_binary(self, payload: bytes) -> None:
        if not self._opened or self.state.media is None:
            # Drop binary frames before ``open`` is ack'd. Sending
            # an error here would force a close mid-stream, which is
            # too aggressive for what's almost always a Genesys-side
            # race during reconnects.
            return
        if self.state.paused:
            # Spec says the client SHOULD NOT send media while
            # paused; if it does (PCI race), drop silently. The
            # paused window exists exactly so we don't transcribe
            # PII.
            return
        try:
            decoded = decode_audio_frame(payload, self.state.media)
        except AudiohookProtocolError:
            # Treat malformed binary frames as protocol errors.
            await self._send_error("invalid-audio", "Malformed binary frame")
            await self.transport.close(code=1002)
            return
        self.state.audio_frames_received += 1
        self.state.audio_bytes_received += len(decoded)
        try:
            await self.sink.on_audio(self.state, decoded)
        except Exception:
            logger.exception("AudioHook sink.on_audio failed")
