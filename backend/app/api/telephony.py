"""Telephony ingress + outbound dial.

Twilio Media Streams flow:

1. Twilio fires ``POST /telephony/twilio/voice?tenant_id={uuid}`` when
   an inbound call arrives on the tenant's Twilio number. We verify
   the ``X-Twilio-Signature`` header, create a LiveSession row, and
   return TwiML telling Twilio to connect the call's audio to our
   WebSocket at ``/ws/telephony/twilio/{session_id}``.

2. Twilio opens that WebSocket and streams base64 μ-law audio in the
   Media Streams JSON protocol. Our handler decodes and forwards to
   Deepgram live transcription, reusing the same coaching + retrieval
   hooks as ``/ws/live/{session_id}``.

3. ``POST /telephony/calls`` places an outbound call via Twilio's REST
   API using the tenant's stored credentials.

Scope limits today:
- Signature validation is enforced when ``TWILIO_AUTH_TOKEN`` is
  configured (so dev setups without it still work). Production must
  set the token.
- Recording / hold / transfer / SignalWire / Telnyx are not implemented
  here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import Response

from backend.app.auth import AuthPrincipal, get_current_principal, require_role
from backend.app.config import get_settings
from backend.app.db import async_session, get_db
from backend.app.models import CallRecording, Integration, Interaction, LiveSession, Tenant
from backend.app.services import s3_audio
from backend.app.services.telephony.twilio import (
    build_hold_twiml,
    build_transfer_twiml,
    build_voice_twiml,
    decode_media_payload,
    validate_twilio_signature,
)
from backend.app.services.token_crypto import decrypt_token


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
    """Resolve Twilio credentials for a tenant.

    Order of precedence:

    1. ``Integration(tenant_id, provider='twilio')`` — ``auth_token`` is
       decrypted from ``access_token``, ``account_sid`` is read from
       ``provider_config['account_sid']``.
    2. Environment variables (``TWILIO_ACCOUNT_SID`` + ``TWILIO_AUTH_TOKEN``)
       — kept for single-tenant dev deployments. Useful when a self-
       hosted instance only runs one tenant.

    Returns a ``TwilioCreds`` with empty strings when nothing is
    configured. Callers decide whether empty creds are fatal (outbound
    dial) or acceptable (webhook sig verify in dev).
    """
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

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Inbound webhook (TwiML) ───────────────────────────────────────────


@router.post("/telephony/twilio/voice")
async def twilio_voice_webhook(
    request: Request,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Twilio hits this URL when a call rings. We return TwiML that
    bridges audio to our Media Streams WebSocket."""
    settings = get_settings()

    raw = await request.body()
    form = await request.form()
    form_dict: Dict[str, str] = {k: str(v) for k, v in form.multi_items()}

    # Validate Twilio's HMAC-SHA1 signature using the **tenant's** auth
    # token (falls back to env vars for single-tenant dev setups). Leave
    # it blank in dev to bypass sig checks so local testing works.
    creds = await _twilio_creds(tenant_id, db)
    if creds.auth_token:
        signature = request.headers.get("X-Twilio-Signature", "")
        # Twilio signs the URL that *they* called — that's what the
        # request looks like from their side, including query string.
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

    # Create the LiveSession up front. The WebSocket handler will find it
    # by id and attach transcription to it.
    session = LiveSession(
        tenant_id=tenant_id,
        # ``agent_id`` is set later when an agent picks up; for now we
        # fall back to the tenant id to satisfy the NOT NULL constraint.
        # Inbound queue/routing would pick a real agent here.
        agent_id=tenant_id,
        source="twilio",
        status="active",
    )
    db.add(session)
    await db.flush()

    base = str(request.base_url).rstrip("/").replace("http://", "wss://").replace(
        "https://", "wss://"
    )
    stream_url = f"{base}/ws/telephony/twilio/{session.id}"

    # Recording opt-in: tenants with audio_storage_enabled get the
    # ``<Start><Recording>`` verb plus a callback URL where Twilio will
    # POST when the audio is ready. AWS must be configured for storage
    # to actually succeed — we don't block the call here, we just log.
    recording_callback: Optional[str] = None
    if tenant.audio_storage_enabled:
        http_base = str(request.base_url).rstrip("/")
        recording_callback = (
            f"{http_base}{settings.API_V1_PREFIX}/telephony/twilio/recording"
            f"?tenant_id={tenant.id}&session_id={session.id}"
        )

    twiml = build_voice_twiml(
        session_id=str(session.id),
        stream_url=stream_url,
        greeting=(
            "This call may be recorded and transcribed for quality assurance."
            if tenant.pii_redaction_enabled or tenant.audio_storage_enabled
            else None
        ),
        record=bool(tenant.audio_storage_enabled),
        recording_status_callback_url=recording_callback,
    )
    return Response(content=twiml, media_type="application/xml")


@router.post("/telephony/twilio/recording")
async def twilio_recording_callback(
    request: Request,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Twilio fires this when a recording is ``completed``.

    Payload (form-encoded): ``RecordingSid``, ``RecordingUrl``,
    ``RecordingDuration``, ``CallSid``, etc. We authenticate the request
    by signature, then mirror the WAV to our own S3 bucket so the audio
    stays under the tenant's control.
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
            raise HTTPException(status_code=403, detail="Invalid signature")

    recording_sid = form_dict.get("RecordingSid", "")
    recording_url = form_dict.get("RecordingUrl", "")
    duration = _safe_int(form_dict.get("RecordingDuration"))

    # Find the linked Interaction (if any) so the recording row can be
    # pivoted off /interactions/{id}/recording.
    sess = await db.get(LiveSession, session_id)
    interaction_id = sess.interaction_id if sess is not None else None

    rec = CallRecording(
        tenant_id=tenant_id,
        interaction_id=interaction_id,
        live_session_id=session_id,
        provider="twilio",
        provider_recording_id=recording_sid,
        status="pending",
        duration_seconds=duration,
    )
    db.add(rec)
    await db.flush()

    # Twilio's recording URL serves the media when you append ".wav" and
    # authenticate with the tenant's Twilio account SID + auth token.
    wav_url = recording_url + ".wav" if recording_url else ""
    if not wav_url:
        rec.status = "failed"
        rec.error = "Twilio callback missing RecordingUrl"
        return {"status": "failed", "recording_id": str(rec.id)}

    try:
        stored = await s3_audio.download_and_store_url(
            tenant_id=tenant_id,
            recording_id=rec.id,
            source_url=wav_url,
            basic_auth=(
                (creds.account_sid, creds.auth_token)
                if creds.account_sid and creds.auth_token
                else None
            ),
        )
        rec.s3_key = stored.s3_key
        rec.size_bytes = stored.size_bytes
        rec.content_type = stored.content_type
        rec.status = "stored"
        rec.stored_at = datetime.now(timezone.utc)
    except s3_audio.S3NotConfigured as exc:
        rec.status = "failed"
        rec.error = f"s3-not-configured: {exc}"
        logger.warning(
            "Recording %s: S3 not configured — keeping placeholder row", rec.id
        )
    except Exception as exc:
        logger.exception("Recording %s upload failed", rec.id)
        rec.status = "failed"
        rec.error = str(exc)[:500]

    return {"status": rec.status, "recording_id": str(rec.id)}


def _safe_int(raw) -> Optional[int]:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


@router.get("/interactions/{interaction_id}/recording")
async def get_recording_playback(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Return a short-lived signed URL for playback of the call audio.

    Any role with access to the tenant can fetch playback — managers use
    this for coaching, compliance uses it for audits. The signed URL TTL
    is 5 minutes so refreshes are frequent and links are hard to leak.
    """
    stmt = (
        select(CallRecording)
        .where(
            CallRecording.tenant_id == principal.tenant.id,
            CallRecording.interaction_id == interaction_id,
            CallRecording.status == "stored",
        )
        .order_by(CallRecording.created_at.desc())
        .limit(1)
    )
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if rec is None or not rec.s3_key:
        raise HTTPException(
            status_code=404, detail="No stored recording for this interaction"
        )
    try:
        url = s3_audio.signed_playback_url(rec.s3_key, ttl_seconds=300)
    except s3_audio.S3NotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "recording_id": str(rec.id),
        "url": url,
        "expires_in": 300,
        "content_type": rec.content_type,
        "duration_seconds": rec.duration_seconds,
        "size_bytes": rec.size_bytes,
    }


# ── Outbound dial ─────────────────────────────────────────────────────


class OutboundDialIn(BaseModel):
    to: str = Field(..., description="E.164 phone number to call")
    from_: Optional[str] = Field(
        default=None,
        alias="from",
        description="Caller ID (must be a verified Twilio number)",
    )
    # Where Twilio should POST for TwiML once the callee answers. Defaults
    # to our inbound webhook so the outbound call goes through the same
    # Media Streams pipe.
    twiml_url: Optional[str] = None

    model_config = {"populate_by_name": True}


class OutboundDialOut(BaseModel):
    sid: str
    status: str


@router.post("/telephony/calls", response_model=OutboundDialOut, status_code=201)
async def place_outbound_call(
    body: OutboundDialIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Place an outbound call via Twilio's REST API.

    Credentials resolve per-tenant via
    ``Integration(tenant_id, provider='twilio')`` first, with the
    ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` env vars as a
    single-tenant-dev fallback.
    """
    creds = await _twilio_creds(principal.tenant.id, db)
    if not (creds.account_sid and creds.auth_token):
        raise HTTPException(
            status_code=503,
            detail=(
                "Twilio credentials are not configured for this tenant. "
                "Connect Twilio in Integrations or set TWILIO_ACCOUNT_SID "
                "+ TWILIO_AUTH_TOKEN env vars."
            ),
        )

    twiml_url = body.twiml_url
    if not twiml_url:
        base = str(request.base_url).rstrip("/")
        twiml_url = f"{base}/api/v1/telephony/twilio/voice?tenant_id={principal.tenant.id}"

    caller_from = body.from_ or ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Calls.json",
            data={
                "To": body.to,
                "From": caller_from,
                "Url": twiml_url,
            },
            auth=(creds.account_sid, creds.auth_token),
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Twilio rejected the call: {resp.status_code} {resp.text[:300]}",
        )
    data = resp.json()
    return OutboundDialOut(
        sid=str(data.get("sid", "")),
        status=str(data.get("status", "")),
    )


# ── Media Streams WebSocket ───────────────────────────────────────────


@router.websocket("/ws/telephony/twilio/{session_id}")
async def twilio_media_stream(websocket: WebSocket, session_id: str):
    """Receive Twilio Media Streams frames and forward audio to Deepgram.

    Twilio sends JSON text frames with these events:

    * ``connected``  — handshake, contains protocol version.
    * ``start``      — call has begun; ``start.mediaFormat`` confirms
      audio encoding (audio/x-mulaw, 8kHz).
    * ``media``      — a base64-encoded audio chunk in ``media.payload``.
    * ``mark``       — marker echo (for sync, not used here).
    * ``stop``       — call ended.

    We decode the payload on each ``media`` frame and push raw bytes
    into our Deepgram live connection, then let the rest of the existing
    live-transcription machinery (coaching, KB lookups, brief alerts,
    outcome inference on close) take over.
    """
    await websocket.accept()

    # Deepgram live connection setup. Twilio streams μ-law at 8 kHz, so
    # tell Deepgram the encoding explicitly.
    settings = get_settings()
    try:
        from deepgram import DeepgramClient
    except Exception:  # pragma: no cover — library is required
        logger.exception("deepgram-sdk missing; closing Twilio WS")
        await websocket.close(code=1011)
        return

    dg_client = DeepgramClient(settings.DEEPGRAM_API_KEY)
    dg_connection = dg_client.listen.live.v("1")

    # Transcripts and coaching hints are handled by the existing live
    # machinery. Here we only care about bridging audio in.
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

    # Mark the LiveSession as live so the monitor view shows it.
    try:
        async with async_session() as db:
            try:
                sess = await db.get(LiveSession, uuid.UUID(session_id))
                if sess is not None:
                    sess.status = "live"
            except Exception:
                pass
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
            # connected / mark / dtmf / anything else: ignored here.
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Twilio media-stream handler crashed for %s", session_id)
    finally:
        try:
            await dg_connection.finish()
        except Exception:
            pass
        # Signal end-of-call to the live machinery. Today the
        # ``_dispatch_batch_analysis`` path lives inside the
        # ``/ws/live/{id}`` handler; for Twilio ingress we kick off the
        # same pipeline by creating an Interaction row from the transcript
        # buffer if the LiveSession has one. The existing
        # ``_dispatch_batch_analysis`` does exactly that — call it.
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


# ── Hold / resume / transfer ──────────────────────────────────────────


class HoldIn(BaseModel):
    music_url: Optional[str] = None


class TransferIn(BaseModel):
    to: str = Field(..., description="E.164 number the call should be transferred to")
    caller_id: Optional[str] = None


async def _twilio_update_call(
    call_sid: str,
    creds: TwilioCreds,
    *,
    twiml: Optional[str] = None,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    """Twilio REST: ``POST /Calls/{sid}.json``. Either ``Twiml`` (inline)
    or ``Url`` (absolute URL to GET TwiML from) must be set. Credentials
    come from the caller (resolved via ``_twilio_creds`` for the tenant)."""
    if not (creds.account_sid and creds.auth_token):
        raise HTTPException(
            status_code=503,
            detail="Twilio credentials are not configured for this tenant",
        )
    if not (twiml or url):
        raise ValueError("Pass either twiml or url to _twilio_update_call")

    data: Dict[str, str] = {}
    if twiml:
        data["Twiml"] = twiml
    else:
        data["Url"] = url  # type: ignore[assignment]

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{creds.account_sid}/Calls/{call_sid}.json",
            data=data,
            auth=(creds.account_sid, creds.auth_token),
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Twilio call update failed: {resp.status_code} {resp.text[:300]}",
        )
    return resp.json()


@router.post("/telephony/calls/{call_sid}/hold")
async def hold_call(
    call_sid: str,
    body: HoldIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Put an in-progress call on hold.

    Replaces the call's current TwiML with a ``<Play loop="0">`` of hold
    music. ``POST /telephony/calls/{sid}/resume`` re-enters the main flow
    by pointing the call back at our inbound voice webhook.
    """
    creds = await _twilio_creds(principal.tenant.id, db)
    twiml = build_hold_twiml(
        hold_music_url=body.music_url
        or "https://com.twilio.sounds.music.s3.amazonaws.com/ClockworkWaltz.mp3"
    )
    result = await _twilio_update_call(call_sid, creds, twiml=twiml)
    return {"status": result.get("status"), "call_sid": call_sid, "state": "hold"}


@router.post("/telephony/calls/{call_sid}/resume")
async def resume_call(
    call_sid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Take a held call off hold by redirecting it back to our voice
    webhook. The webhook creates a fresh LiveSession and resumes the
    live transcription + coaching flow."""
    creds = await _twilio_creds(principal.tenant.id, db)
    base = str(request.base_url).rstrip("/")
    voice_url = (
        f"{base}{get_settings().API_V1_PREFIX}/telephony/twilio/voice"
        f"?tenant_id={principal.tenant.id}"
    )
    result = await _twilio_update_call(call_sid, creds, url=voice_url)
    return {"status": result.get("status"), "call_sid": call_sid, "state": "active"}


@router.post("/telephony/calls/{call_sid}/transfer")
async def transfer_call(
    call_sid: str,
    body: TransferIn,
    db: AsyncSession = Depends(get_db),
    principal: AuthPrincipal = Depends(
        require_role("manager")
    ),
):
    """Cold-transfer the call to another number.

    Managers + admins only — agents can't silently hand off a call
    without manager approval. For warm transfer, the agent should use
    hold + a separate outbound dial, then transfer once the new party
    agrees to take it (that flow can be layered on top of these
    primitives).
    """
    creds = await _twilio_creds(principal.tenant.id, db)
    twiml = build_transfer_twiml(to_number=body.to, caller_id=body.caller_id)
    result = await _twilio_update_call(call_sid, creds, twiml=twiml)
    return {
        "status": result.get("status"),
        "call_sid": call_sid,
        "state": "transferring",
        "to": body.to,
    }


# ── SignalWire (TwiML-compatible) ─────────────────────────────────────


def _signalwire_creds_for_tenant(tenant: Tenant, db_sync_integ=None) -> Dict[str, Any]:
    """Return {project_id, api_token, space_url} for the tenant.

    Resolution order: tenant Integration row (provider='signalwire')
    first, with the tokens decrypted on the way out; fall back to env
    vars for single-tenant deployments.
    """
    from backend.app.services.token_crypto import decrypt_token

    settings = get_settings()
    if db_sync_integ is not None:
        cfg = db_sync_integ.provider_config or {}
        return {
            "project_id": settings.SIGNALWIRE_PROJECT_ID or cfg.get("project_id", ""),
            "api_token": decrypt_token(db_sync_integ.access_token) or settings.SIGNALWIRE_TOKEN or "",
            "space_url": cfg.get("space_url", ""),
        }
    return {
        "project_id": settings.SIGNALWIRE_PROJECT_ID,
        "api_token": settings.SIGNALWIRE_TOKEN,
        "space_url": "",  # env-config path: caller must set via request.query or integration
    }


@router.post("/telephony/signalwire/voice")
async def signalwire_voice_webhook(
    request: Request,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Inbound SignalWire webhook. SignalWire sends the same form-encoded
    payload Twilio does and signs it with HMAC-SHA1, so we can reuse the
    Twilio helpers.

    Auth token for signature verification comes from the tenant's
    Integration row (provider='signalwire'); we fall back to
    ``SIGNALWIRE_TOKEN`` env var for single-tenant deployments.
    """
    from backend.app.models import Integration

    form = await request.form()
    form_dict: Dict[str, str] = {k: str(v) for k, v in form.multi_items()}

    # Resolve auth token for signature validation.
    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "signalwire",
        )
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    auth_token = ""
    if integ is not None:
        from backend.app.services.token_crypto import decrypt_token

        auth_token = decrypt_token(integ.access_token) or ""
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
    twiml = build_voice_twiml(
        session_id=str(session.id),
        stream_url=stream_url,
        greeting=(
            "This call may be recorded and transcribed for quality assurance."
            if tenant.pii_redaction_enabled or tenant.audio_storage_enabled
            else None
        ),
    )
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/ws/telephony/signalwire/{session_id}")
async def signalwire_media_stream(websocket: WebSocket, session_id: str):
    """SignalWire's Compatibility API streams audio in the same format
    Twilio does. Just delegate to the Twilio handler implementation."""
    # Reuse the Twilio handler — it's identical byte-for-byte.
    await twilio_media_stream(websocket, session_id)


# ── Telnyx Call Control ───────────────────────────────────────────────


async def _telnyx_api_key_for_tenant(tenant_id: uuid.UUID, db: AsyncSession) -> Optional[str]:
    """Decrypt the tenant's Telnyx API key from Integration, if present."""
    from backend.app.models import Integration
    from backend.app.services.token_crypto import decrypt_token

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
    from backend.app.models import Integration

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


async def _telnyx_post(api_key: str, url: str, json: Optional[dict] = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            json=json or {},
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

    Handles the event types we need:

    * ``call.initiated`` — answer the call, then kick off media streaming
      to our WS by issuing ``streaming_start``.
    * ``call.hangup`` — dispatch batch analysis (same as Twilio stop).

    Other events (call.answered, streaming.started, etc.) are
    acknowledged with 200 and ignored.
    """
    from backend.app.services.telephony.telnyx import (
        call_control_answer_url,
        call_control_streaming_start_url,
        streaming_start_payload,
        verify_telnyx_signature,
    )

    raw = await request.body()

    # Signature verification. Telnyx signs with Ed25519; the public key
    # lives in provider_config. When it's not configured we log and
    # accept (dev-only posture — same as Twilio).
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

    import json as _json

    try:
        payload = _json.loads(raw)
    except _json.JSONDecodeError:
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
        # Create LiveSession so the downstream WS can attach to it.
        session = LiveSession(
            tenant_id=tenant_id,
            agent_id=tenant_id,
            source="telnyx",
            status="active",
        )
        db.add(session)
        await db.flush()

        # Answer the call, then start streaming to us.
        await _telnyx_post(api_key, call_control_answer_url(call_control_id))
        http_base = str(request.base_url).rstrip("/")
        wss_base = http_base.replace("http://", "wss://").replace("https://", "wss://")
        stream_url = (
            f"{wss_base}/ws/telephony/telnyx/{session.id}"
        )
        await _telnyx_post(
            api_key,
            call_control_streaming_start_url(call_control_id),
            json=streaming_start_payload(stream_url=stream_url),
        )
        return {"status": "streaming_started", "session_id": str(session.id)}

    if event_type == "call.hangup" and call_control_id:
        # Best-effort: locate the LiveSession by recent tenant + source
        # and dispatch batch analysis. Telnyx doesn't give us the session
        # id on this event, so we fall back to "most recent active session
        # for the tenant on telnyx".
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
        return {"status": "hangup_handled"}

    # Unhandled events get a polite 200 so Telnyx doesn't retry forever.
    return {"status": "ignored", "event_type": event_type}


@router.websocket("/ws/telephony/telnyx/{session_id}")
async def telnyx_media_stream(websocket: WebSocket, session_id: str):
    """Telnyx Media Streaming WebSocket.

    Like Twilio, Telnyx sends JSON frames with ``event: media`` + a
    base64-encoded ``media.payload`` (μ-law 8kHz by default).
    Structurally close enough that we can reuse the Twilio handler
    verbatim — our decode helper is format-neutral.
    """
    await twilio_media_stream(websocket, session_id)
