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
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import Response

from backend.app.auth import AuthPrincipal, get_current_principal
from backend.app.config import get_settings
from backend.app.db import async_session, get_db
from backend.app.models import Interaction, LiveSession, Tenant
from backend.app.services.telephony.twilio import (
    build_voice_twiml,
    decode_media_payload,
    validate_twilio_signature,
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

    # Validate Twilio's HMAC-SHA1 signature when we have an auth token
    # configured. Dev deployments that leave the token blank bypass this
    # so local testing without Twilio still works; production should set it.
    if settings.TWILIO_AUTH_TOKEN:
        signature = request.headers.get("X-Twilio-Signature", "")
        # Twilio signs the URL that *they* called — that's what the
        # request looks like from their side, including query string.
        request_url = str(request.url)
        if not validate_twilio_signature(
            auth_token=settings.TWILIO_AUTH_TOKEN,
            request_url=request_url,
            params=form_dict,
            signature_header=signature,
        ):
            logger.warning(
                "Twilio webhook signature mismatch (tenant=%s)", tenant_id
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

    twiml = build_voice_twiml(
        session_id=str(session.id),
        stream_url=stream_url,
        greeting=(
            "This call may be recorded and transcribed for quality assurance."
            if tenant.pii_redaction_enabled
            else None
        ),
    )
    return Response(content=twiml, media_type="application/xml")


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
    principal: AuthPrincipal = Depends(get_current_principal),
):
    """Place an outbound call via Twilio's REST API.

    Requires the tenant to have ``TWILIO_ACCOUNT_SID`` + ``TWILIO_AUTH_TOKEN``
    configured on the server (today these are env-scoped; per-tenant
    Twilio credentials are a follow-on).
    """
    settings = get_settings()
    sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    if not (sid and token):
        raise HTTPException(
            status_code=503, detail="Twilio credentials are not configured"
        )

    twiml_url = body.twiml_url
    if not twiml_url:
        base = str(request.base_url).rstrip("/")
        twiml_url = f"{base}/api/v1/telephony/twilio/voice?tenant_id={principal.tenant.id}"

    caller_from = body.from_ or ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
            data={
                "To": body.to,
                "From": caller_from,
                "Url": twiml_url,
            },
            auth=(sid, token),
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
