"""Telephony ingress — live-stream transcription only.

LINDA does not originate calls, place them on hold, or transfer them.
Those controls live in the tenant's phone system (contact center, MiaRec,
Teams, MetaSwitch, etc.). What we expose here is narrow:

1. **Inbound voice webhooks** (Twilio / SignalWire / Telnyx) — return
   TwiML (or the provider equivalent) that bridges the live audio into
   our Media Streams WebSocket so we can transcribe + coach in real
   time.
2. **Media Streams WebSockets** — accept base64 μ-law frames from the
   provider, forward to Deepgram live, and dispatch batch analysis when
   the call ends. Audio bytes are never persisted to disk or object
   storage.
3. **Admin**: link the tenant's Twilio credentials (used for inbound
   signature verification).

Post-call recordings from tenant recording systems come in through
``POST /interactions/ingest-recording`` (see ``backend.app.api.interactions``),
not here.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import Response

from backend.app.auth import AuthPrincipal, get_current_principal, require_role
from backend.app.config import get_settings
from backend.app.db import async_session, get_db
from backend.app.models import Integration, LiveSession, Tenant
from backend.app.services.telephony.twilio import (
    build_voice_twiml,
    decode_media_payload,
    validate_twilio_signature,
)
from backend.app.services.token_crypto import decrypt_token

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class TwilioCreds:
    """Resolved per-tenant Twilio credentials. ``source`` is ``"tenant"``
    when the Integration row provided them or ``"env"`` for the
    single-tenant dev fallback."""

    account_sid: str
    auth_token: str
    source: str


async def _twilio_creds(
    tenant_id: uuid.UUID, db: AsyncSession
) -> TwilioCreds:
    """Resolve Twilio credentials for a tenant. Used only for webhook
    signature verification — we no longer originate calls."""
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.provider == "twilio")
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is not None:
        cfg = integ.provider_config or {}
        account_sid = str(cfg.get("account_sid") or "")
        auth_token = decrypt_token(integ.access_token) or ""
        if account_sid and auth_token:
            return TwilioCreds(
                account_sid=account_sid, auth_token=auth_token, source="tenant"
            )

    settings = get_settings()
    return TwilioCreds(
        account_sid=settings.TWILIO_ACCOUNT_SID or "",
        auth_token=settings.TWILIO_AUTH_TOKEN or "",
        source="env",
    )


# ── Inbound webhook (TwiML) ───────────────────────────────────────────


@router.post("/telephony/twilio/voice")
async def twilio_voice_webhook(
    request: Request,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Twilio hits this URL when a call rings. We return TwiML that
    bridges audio to our Media Streams WebSocket.

    Audio is transcribed in real time and discarded — recordings (if any)
    are produced by the tenant's own recording system and POSTed to
    ``/interactions/ingest-recording`` as a separate flow.
    """
    form = await request.form()
    form_dict: Dict[str, str] = {k: str(v) for k, v in form.multi_items()}

    creds = await _twilio_creds(tenant_id, db)
    if creds.auth_token:
        signature = request.headers.get("X-Twilio-Signature", "")
        request_url = str(request.url)
        if not validate_twilio_signature(
            auth_token=creds.auth_token,
            request_url=request_url,
            params=form_dict,
            signature_header=signature,
        ):
            logger.warning(
                "Twilio webhook signature mismatch (tenant=%s, creds_source=%s)",
                tenant_id,
                creds.source,
            )
            raise HTTPException(status_code=403, detail="Invalid signature")

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    session = LiveSession(
        tenant_id=tenant_id,
        agent_id=tenant_id,  # populated when an agent claims the session
        source="twilio",
        status="active",
    )
    db.add(session)
    await db.flush()

    base = str(request.base_url).rstrip("/").replace("http://", "wss://").replace(
        "https://", "wss://"
    )
    stream_url = f"{base}/ws/telephony/twilio/{session.id}"

    greeting: Optional[str] = None
    if getattr(tenant, "pii_redaction_enabled", False):
        greeting = (
            "This call may be transcribed for quality assurance. "
            "Audio is not retained."
        )

    twiml = build_voice_twiml(
        session_id=str(session.id),
        stream_url=stream_url,
        greeting=greeting,
    )
    return Response(content=twiml, media_type="application/xml")


# ── Media Streams WebSocket ───────────────────────────────────────────


@router.websocket("/ws/telephony/twilio/{session_id}")
async def twilio_media_stream(websocket: WebSocket, session_id: str):
    """Receive Twilio Media Streams frames and forward audio to Deepgram.

    Twilio sends JSON text frames with these events:

    * ``connected`` — handshake, contains protocol version.
    * ``start`` — call has begun; ``start.mediaFormat`` confirms audio
      encoding (audio/x-mulaw, 8kHz).
    * ``media`` — a base64-encoded audio chunk in ``media.payload``.
    * ``mark`` — marker echo (for sync, not used here).
    * ``stop`` — call ended.

    We decode the payload on each ``media`` frame, push raw bytes into
    the Deepgram live connection, and throw them away — nothing ever
    reaches disk or S3.
    """
    await websocket.accept()

    settings = get_settings()
    try:
        from deepgram import DeepgramClient
    except Exception:  # pragma: no cover — library is required
        logger.exception("deepgram-sdk missing; closing Twilio WS")
        await websocket.close(code=1011)
        return

    dg_client = DeepgramClient(settings.DEEPGRAM_API_KEY)
    dg_connection = dg_client.listen.live.v("1")
    try:
        await dg_connection.start(
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
        logger.exception("Failed to start Deepgram connection for session %s", session_id)
        await websocket.close(code=1011)
        return

    try:
        async with async_session() as db:
            sess = await db.get(LiveSession, uuid.UUID(session_id))
            if sess is not None:
                sess.status = "live"
    except Exception:
        logger.debug("Couldn't update LiveSession.status", exc_info=True)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = frame.get("event")
            if event == "media":
                payload = (frame.get("media") or {}).get("payload") or ""
                audio = decode_media_payload(payload)
                if audio:
                    await dg_connection.send(audio)
            elif event == "stop":
                break
            elif event == "start":
                logger.info(
                    "Twilio stream started for session %s: %s",
                    session_id,
                    frame.get("start", {}).get("mediaFormat"),
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Twilio media-stream handler crashed for %s", session_id)
    finally:
        try:
            await dg_connection.finish()
        except Exception:
            pass
        try:
            from backend.app.api.websocket import _dispatch_batch_analysis
            import redis.asyncio as aioredis

            redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            try:
                await _dispatch_batch_analysis(redis, session_id)
            finally:
                await redis.aclose()
        except Exception:
            logger.exception("Batch analysis dispatch failed for %s", session_id)


# ── SignalWire (TwiML-compatible) ─────────────────────────────────────


@router.post("/telephony/signalwire/voice")
async def signalwire_voice_webhook(
    request: Request,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Inbound SignalWire webhook. SignalWire sends the same form-encoded
    payload Twilio does and signs it with HMAC-SHA1, so we reuse the
    Twilio signature helper and TwiML builder."""
    form = await request.form()
    form_dict: Dict[str, str] = {k: str(v) for k, v in form.multi_items()}

    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "signalwire",
        )
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    auth_token = decrypt_token(integ.access_token) or "" if integ is not None else ""
    if not auth_token:
        auth_token = get_settings().SIGNALWIRE_TOKEN or ""

    if auth_token:
        signature = request.headers.get("X-Twilio-Signature", "")  # SignalWire reuses this header
        if not validate_twilio_signature(
            auth_token=auth_token,
            request_url=str(request.url),
            params=form_dict,
            signature_header=signature,
        ):
            raise HTTPException(status_code=403, detail="Invalid signature")

    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    session = LiveSession(
        tenant_id=tenant_id,
        agent_id=tenant_id,
        source="signalwire",
        status="active",
    )
    db.add(session)
    await db.flush()

    base = str(request.base_url).rstrip("/").replace("http://", "wss://").replace(
        "https://", "wss://"
    )
    stream_url = f"{base}/ws/telephony/signalwire/{session.id}"
    greeting: Optional[str] = None
    if getattr(tenant, "pii_redaction_enabled", False):
        greeting = (
            "This call may be transcribed for quality assurance. "
            "Audio is not retained."
        )
    twiml = build_voice_twiml(
        session_id=str(session.id),
        stream_url=stream_url,
        greeting=greeting,
    )
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/ws/telephony/signalwire/{session_id}")
async def signalwire_media_stream(websocket: WebSocket, session_id: str):
    """SignalWire's Compatibility API streams audio in the same format
    Twilio does. Delegate to the Twilio handler."""
    await twilio_media_stream(websocket, session_id)


# ── Telnyx Call Control (inbound + live streaming only) ───────────────


_TELNYX_SESSION_KEY_PREFIX = "telephony:telnyx:call"
_TELNYX_SESSION_TTL_SECONDS = 12 * 3600


async def _telnyx_remember_session(
    call_control_id: str, session_id: uuid.UUID
) -> None:
    import redis.asyncio as aioredis

    try:
        r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            await r.set(
                f"{_TELNYX_SESSION_KEY_PREFIX}:{call_control_id}",
                str(session_id),
                ex=_TELNYX_SESSION_TTL_SECONDS,
            )
        finally:
            await r.aclose()
    except Exception:
        logger.debug("Telnyx session map write failed", exc_info=True)


async def _telnyx_lookup_session(call_control_id: str) -> Optional[uuid.UUID]:
    import redis.asyncio as aioredis

    try:
        r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            raw = await r.get(f"{_TELNYX_SESSION_KEY_PREFIX}:{call_control_id}")
        finally:
            await r.aclose()
        if not raw:
            return None
        try:
            return uuid.UUID(raw)
        except ValueError:
            return None
    except Exception:
        logger.debug("Telnyx session map read failed", exc_info=True)
        return None


async def _telnyx_forget_session(call_control_id: str) -> None:
    import redis.asyncio as aioredis

    try:
        r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
        try:
            await r.delete(f"{_TELNYX_SESSION_KEY_PREFIX}:{call_control_id}")
        finally:
            await r.aclose()
    except Exception:
        logger.debug("Telnyx session map delete failed", exc_info=True)


async def _telnyx_api_key_for_tenant(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[str]:
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.provider == "telnyx")
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        return None
    return decrypt_token(integ.access_token)


async def _telnyx_public_key_for_tenant(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[str]:
    stmt = (
        select(Integration)
        .where(Integration.tenant_id == tenant_id, Integration.provider == "telnyx")
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        return None
    return (integ.provider_config or {}).get("public_key")


async def _telnyx_post(api_key: str, url: str, json_body: Optional[dict] = None) -> Dict:
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            json=json_body or {},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Telnyx API failed: {resp.status_code} {resp.text[:300]}",
        )
    return resp.json() if resp.content else {}


@router.post("/telephony/telnyx/voice")
async def telnyx_voice_webhook(
    request: Request,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Telnyx Call Control webhook.

    * ``call.initiated`` — answer, start streaming audio to our WS.
    * ``call.hangup`` — dispatch batch analysis.

    Other events are acknowledged with 200 and ignored.
    """
    from backend.app.services.telephony.telnyx import (
        call_control_answer_url,
        call_control_streaming_start_url,
        streaming_start_payload,
        verify_telnyx_signature,
    )

    raw = await request.body()

    public_key = await _telnyx_public_key_for_tenant(tenant_id, db)
    if public_key:
        sig = request.headers.get("Telnyx-Signature-Ed25519", "")
        ts = request.headers.get("Telnyx-Timestamp", "")
        if not verify_telnyx_signature(
            public_key_base64=public_key,
            signature_header=sig,
            timestamp_header=ts,
            raw_body=raw,
        ):
            raise HTTPException(status_code=403, detail="Invalid Telnyx signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    data = (payload.get("data") or {}).get("payload") or payload.get("data") or {}
    event_type = (payload.get("data") or {}).get("event_type") or payload.get("event_type")
    call_control_id = data.get("call_control_id") or data.get("call_leg_id") or ""

    api_key = await _telnyx_api_key_for_tenant(tenant_id, db)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Telnyx integration not connected for this tenant",
        )

    if event_type == "call.initiated" and call_control_id:
        session = LiveSession(
            tenant_id=tenant_id,
            agent_id=tenant_id,
            source="telnyx",
            status="active",
        )
        db.add(session)
        await db.flush()

        await _telnyx_remember_session(call_control_id, session.id)

        await _telnyx_post(api_key, call_control_answer_url(call_control_id))
        http_base = str(request.base_url).rstrip("/")
        wss_base = http_base.replace("http://", "wss://").replace("https://", "wss://")
        stream_url = f"{wss_base}/ws/telephony/telnyx/{session.id}"
        await _telnyx_post(
            api_key,
            call_control_streaming_start_url(call_control_id),
            json_body=streaming_start_payload(stream_url=stream_url),
        )
        return {"status": "streaming_started", "session_id": str(session.id)}

    if event_type == "call.hangup" and call_control_id:
        session_id = await _telnyx_lookup_session(call_control_id)
        sess = None
        if session_id is not None:
            sess = await db.get(LiveSession, session_id)
        if sess is None:
            stmt = (
                select(LiveSession)
                .where(
                    LiveSession.tenant_id == tenant_id,
                    LiveSession.source == "telnyx",
                    LiveSession.ended_at.is_(None),
                )
                .order_by(LiveSession.started_at.desc())
                .limit(1)
            )
            sess = (await db.execute(stmt)).scalar_one_or_none()
        if sess is not None:
            try:
                from backend.app.api.websocket import _dispatch_batch_analysis
                import redis.asyncio as aioredis

                settings = get_settings()
                redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
                try:
                    await _dispatch_batch_analysis(redis, str(sess.id))
                finally:
                    await redis.aclose()
            except Exception:
                logger.exception("Telnyx hangup dispatch failed")

        await _telnyx_forget_session(call_control_id)
        return {"status": "hangup_handled"}

    return {"status": "ignored", "event_type": event_type}


@router.websocket("/ws/telephony/telnyx/{session_id}")
async def telnyx_media_stream(websocket: WebSocket, session_id: str):
    """Telnyx Media Streaming WebSocket. Same JSON framing as Twilio —
    delegate to the Twilio handler."""
    await twilio_media_stream(websocket, session_id)


# ── Admin: link per-tenant Twilio credentials ─────────────────────────


class TwilioCredsIn(BaseModel):
    account_sid: str = Field(..., pattern=r"^AC[a-zA-Z0-9]+$")
    auth_token: str = Field(..., min_length=8)


@router.post("/admin/integrations/twilio")
async def link_twilio_credentials(
    body: TwilioCredsIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(require_role("admin")),
):
    """Save the tenant's Twilio account SID + auth token.

    Used only for webhook signature verification. The auth token is
    Fernet-encrypted at rest; the SID sits in ``provider_config``.
    """
    from backend.app.services.token_crypto import encrypt_token

    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == principal.tenant.id,
            Integration.provider == "twilio",
        )
        .limit(1)
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is None:
        integ = Integration(
            tenant_id=principal.tenant.id,
            user_id=principal.user_id or principal.tenant.id,
            provider="twilio",
            access_token=encrypt_token(body.auth_token),
            refresh_token=None,
            scopes=[],
            provider_config={"account_sid": body.account_sid},
        )
        db.add(integ)
        await db.flush()
    else:
        existing.access_token = encrypt_token(body.auth_token)
        cfg = dict(existing.provider_config or {})
        cfg["account_sid"] = body.account_sid
        existing.provider_config = cfg

    return {
        "provider": "twilio",
        "account_sid": body.account_sid,
        "saved": True,
    }
