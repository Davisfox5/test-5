"""Genesys Cloud AudioHook ingress — Stream 4 of multi-stream telephony work.

Exposes a single WebSocket endpoint that accepts a Genesys-signed
upgrade request, runs the AudioHook protocol state machine, and
funnels decoded audio into the Deepgram-backed live transcription
pipeline. No outbound calls; no recording persistence — audio bytes
are forwarded and discarded, mirroring the existing Twilio /
SignalWire / Telnyx Media Streams ingress.

Auth model
----------

AudioHook is HMAC-SHA256, NOT OAuth. Each tenant provisions an
:class:`Integration` row with ``provider="genesys_audiohook"`` whose
``provider_config`` carries:

* ``api_key`` — the X-API-KEY value Genesys sends with every upgrade.
* ``client_secret`` — encrypted via :mod:`services.token_crypto`,
  used as the HMAC key for signature verification.

The verification step is :func:`backend.app.services.telephony.audiohook.auth.verify_audiohook_signature`.
We deliberately do this BEFORE :meth:`WebSocket.accept` so a
malformed or tampered request never gets a 101 response.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db import async_session
from backend.app.models import AudiohookSession as AudiohookSessionRow
from backend.app.models import Integration
from backend.app.services.audio.normalizer import AudioFormat, to_mulaw_8k
from backend.app.services.telephony.audiohook.auth import (
    SignatureVerificationError,
    verify_audiohook_signature,
)
from backend.app.services.telephony.audiohook.server import (
    AudioSink,
    AudiohookSession,
    AudiohookSessionState,
)
from backend.app.services.token_crypto import decrypt_token
from backend.app.tenant_ctx import reset_current_tenant, set_current_tenant


logger = logging.getLogger(__name__)

router = APIRouter()


# ── Mid-call reconnect position store ──────────────────────────────────


class _RedisAudiohookPositionStore:
    """Redis-backed :class:`PositionStore` keyed by (tenant,
    conversation). Genesys reconnects mid-call with a fresh WebSocket
    and session id but the same ``conversationId`` — the stored
    position lets the new connection continue the conversation's
    timeline instead of restarting at zero."""

    _TTL_SECONDS = 3600

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url

    def _key(self, state: AudiohookSessionState) -> str:
        return f"audiohook:pos:{state.tenant_id}:{state.conversation_id}"

    async def _with_redis(self, fn):
        import redis.asyncio as aioredis

        redis = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            return await fn(redis)
        finally:
            await redis.aclose()

    async def load(self, state: AudiohookSessionState) -> float:
        if not state.conversation_id:
            return 0.0

        async def _load(redis):
            raw = await redis.get(self._key(state))
            try:
                return float(raw) if raw is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        return await self._with_redis(_load)

    async def save(self, state: AudiohookSessionState) -> None:
        if not state.conversation_id:
            return

        async def _save(redis):
            await redis.set(
                self._key(state),
                repr(state.audio_position_sec()),
                ex=self._TTL_SECONDS,
            )

        await self._with_redis(_save)

    async def clear(self, state: AudiohookSessionState) -> None:
        if not state.conversation_id:
            return

        async def _clear(redis):
            await redis.delete(self._key(state))

        await self._with_redis(_clear)


# ── Audio sink wired to Deepgram live + paralinguistic window ──────────


class _LiveTranscriptionSink:
    """Production sink: feeds decoded audio into the same Deepgram
    streaming connection the Twilio path uses.

    Lazy-imports the Deepgram SDK and the paralinguistic window so
    the route module stays import-safe in test environments without
    those dependencies. The connection is created on ``on_open`` and
    finalized in ``on_close``; pause/resume don't tear it down — we
    just stop feeding bytes during the paused window so there's no
    transcript gap the agent has to notice.
    """

    def __init__(self) -> None:
        self._dg_connection: Any = None

    async def on_open(self, session: AudiohookSessionState) -> None:
        if session.media is None:
            return
        try:
            from backend.app.config import get_settings
            from deepgram import DeepgramClient
        except Exception:
            logger.exception("AudioHook: deepgram-sdk missing; running without live transcription")
            return
        settings = get_settings()
        client = DeepgramClient(settings.DEEPGRAM_API_KEY)
        self._dg_connection = client.listen.live.v("1")
        try:
            # We always feed Deepgram μ-law 8 kHz to match the Twilio
            # path. AudioHook L16 is converted in ``on_audio``.
            await self._dg_connection.start(
                {
                    "model": "nova-3",
                    "encoding": "mulaw",
                    "sample_rate": 8000,
                    "channels": 1,
                    "interim_results": True,
                    "diarize": True,
                }
            )
        except Exception:
            logger.exception("AudioHook: failed to start Deepgram connection")
            self._dg_connection = None

    async def on_audio(
        self, session: AudiohookSessionState, payload: bytes
    ) -> None:
        if self._dg_connection is None or session.media is None:
            return
        # Decoded payload is either μ-law 8k passthrough (PCMU) or
        # PCM16 little-endian at the negotiated rate. Normalize to
        # μ-law 8k for Deepgram.
        try:
            audio_format = session.media.to_audio_format()
        except ValueError:
            return
        if audio_format == AudioFormat.MULAW_8K:
            mulaw = payload
        else:
            mulaw = to_mulaw_8k(payload, audio_format)
        try:
            await self._dg_connection.send(mulaw)
        except Exception:
            logger.debug("AudioHook: Deepgram send failed", exc_info=True)

    async def on_paused(self, session: AudiohookSessionState) -> None:
        # Nothing to do — ``on_audio`` is gated by ``state.paused``
        # in the session loop, so no bytes flow during the pause.
        pass

    async def on_resumed(self, session: AudiohookSessionState) -> None:
        pass

    async def on_close(self, session: AudiohookSessionState) -> None:
        if self._dg_connection is None:
            return
        try:
            await self._dg_connection.finish()
        except Exception:
            logger.debug("AudioHook: Deepgram finish failed", exc_info=True)
        self._dg_connection = None


# ── Persistence callbacks (DB-bound) ───────────────────────────────────


async def _persist_open(
    state: AudiohookSessionState, db: AsyncSession
) -> uuid.UUID:
    row = AudiohookSessionRow(
        tenant_id=uuid.UUID(state.tenant_id),
        audiohook_session_id=state.session_id,
        organization_id=state.organization_id or None,
        conversation_id=state.conversation_id or None,
        participant_id=state.participant_id or None,
        channel=state.channel or "unknown",
        media_format=(
            {
                "format": state.media.format,
                "rate": state.media.rate,
                "channels": list(state.media.channels),
            }
            if state.media
            else {}
        ),
        started_at=state.started_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


async def _persist_close(
    state: AudiohookSessionState,
    persisted_id: Optional[uuid.UUID],
    db: AsyncSession,
) -> None:
    if persisted_id is None:
        return
    row = await db.get(AudiohookSessionRow, persisted_id)
    if row is None:
        return
    row.ended_at = state.ended_at
    row.audio_frames_received = state.audio_frames_received
    row.audio_bytes_received = state.audio_bytes_received
    row.channel = state.channel or "unknown"
    await db.commit()


# ── Tenant secret resolution ───────────────────────────────────────────


async def _resolve_audiohook_secret(
    tenant_id: uuid.UUID, db: AsyncSession
) -> Optional[str]:
    """Look up the per-tenant AudioHook HMAC secret.

    Returns ``None`` when the tenant has no AudioHook integration —
    the caller treats that as 401, not 404, because we don't want to
    leak which tenants are onboarded.
    """

    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "genesys_audiohook",
        )
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        return None
    # Two storage shapes are accepted: secret in ``access_token``
    # (encrypted via ``token_crypto``) OR in ``provider_config.client_secret``.
    # The admin endpoint that creates the integration is owned by a
    # follow-up; both shapes let either implementation work without
    # forcing a migration.
    if integ.access_token:
        plain = decrypt_token(integ.access_token)
        if plain:
            return plain
    cfg = integ.provider_config or {}
    raw_secret = cfg.get("client_secret")
    if isinstance(raw_secret, str) and raw_secret:
        return raw_secret
    return None


# ── WebSocket transport adapter ────────────────────────────────────────


class _FastAPITransport:
    """Adapt :class:`fastapi.WebSocket` to :class:`AudiohookTransport`."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    async def receive(self) -> dict:
        return await self.ws.receive()

    async def send_text(self, data: str) -> None:
        await self.ws.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def close(self, code: int = 1000) -> None:
        try:
            await self.ws.close(code=code)
        except Exception:
            # Already closed — Starlette raises if you double-close.
            pass


# ── Public WebSocket endpoint ──────────────────────────────────────────


@router.websocket("/audiohook/{tenant_id}")
async def audiohook_endpoint(
    websocket: WebSocket,
    tenant_id: str,
    sink_factory: Optional[Callable[[], AudioSink]] = None,
) -> None:
    """Accept a Genesys AudioHook WebSocket upgrade.

    URL path: ``/api/v1/audiohook/{tenant_id}``. The ``tenant_id``
    is the tenant's LINDA UUID — Genesys sends it in the ``X-API-KEY``
    header too, but we use the path component for routing because
    AudioHook integrations are per-tenant 1:1 today.

    Verification flow:

    1. Parse the upgrade headers.
    2. Look up the tenant's HMAC secret via
       :func:`_resolve_audiohook_secret`.
    3. Verify the signature with
       :func:`verify_audiohook_signature`. On failure, reject the
       upgrade with a 401-equivalent close (Starlette doesn't expose
       a clean way to refuse the upgrade pre-accept, so we accept
       and immediately close 1008 — Genesys treats both as auth
       failures).
    4. Accept the WebSocket and hand off to :class:`AudiohookSession`.
    """

    headers = {k: v for k, v in websocket.headers.items()}
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        await websocket.close(code=1008)
        return

    # DB sessions here are opened per-operation (a fresh ``async_session``
    # for the secret lookup, another for the persistence callbacks), so
    # there's no single AsyncSession to bind_tenant_async on. Instead we
    # bind the ContextVar once, for the lifetime of this connection's
    # asyncio task — the ``after_begin`` listener arms every subsequent
    # transaction (on either session) from it. Reset on every exit path
    # so the token doesn't leak into whatever task runs next.
    token = set_current_tenant(tenant_uuid)
    try:
        async with async_session() as db:
            secret = await _resolve_audiohook_secret(tenant_uuid, db)
        if secret is None:
            # No tenant integration → reject without leaking detail.
            await websocket.close(code=1008)
            return

        # Reconstruct request target / authority for signature base.
        method = "GET"
        target_path = websocket.url.path
        if websocket.url.query:
            target_path = f"{target_path}?{websocket.url.query}"
        authority = headers.get("host") or websocket.url.netloc

        try:
            verify_audiohook_signature(
                method=method,
                target_path=target_path,
                authority=authority,
                headers=headers,
                client_secret=secret,
            )
        except SignatureVerificationError as exc:
            logger.info(
                "AudioHook signature rejected for tenant=%s: %s", tenant_id, exc
            )
            await websocket.close(code=1008)
            return

        await websocket.accept()

        sink = (sink_factory or _LiveTranscriptionSink)()
        transport = _FastAPITransport(websocket)

        # Open per-call DB session for the persistence callbacks. We use
        # a long-lived ``async_session`` because AudioHook sessions can
        # be hours long; the SQLAlchemy session object is reusable and
        # we commit on each callback so there's no buildup of pending
        # state.
        async with async_session() as db:

            async def _persist_open_cb(state: AudiohookSessionState) -> uuid.UUID:
                return await _persist_open(state, db)

            async def _persist_close_cb(
                state: AudiohookSessionState, persisted_id: Optional[uuid.UUID]
            ) -> None:
                await _persist_close(state, persisted_id, db)

            try:
                from backend.app.config import get_settings

                position_store: Optional[_RedisAudiohookPositionStore] = (
                    _RedisAudiohookPositionStore(get_settings().REDIS_URL)
                )
            except Exception:
                logger.debug("AudioHook position store unavailable", exc_info=True)
                position_store = None

            session = AudiohookSession(
                transport=transport,
                sink=sink,
                tenant_id=tenant_id,
                persist_open=_persist_open_cb,
                persist_close=_persist_close_cb,
                position_store=position_store,
            )
            await session.handle()
    finally:
        reset_current_tenant(token)


__all__ = ["router"]
