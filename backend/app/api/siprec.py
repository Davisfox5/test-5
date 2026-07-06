"""SIPREC HTTP control plane.

Two surfaces, both small:

* ``POST /siprec/events`` — webhook the SRS sidecar calls when a
  recording starts, stops, or an audio frame batch arrives. Auth is a
  shared secret in the ``X-SRS-Token`` header (the SRS lives on the
  same Fly app, so we trust the private network + a rotating secret
  rather than minting a public OAuth credential just for it).
* ``POST /admin/integrations/siprec`` — tenant admin configures the
  per-tenant Integration row: which SBCs (by IP allowlist) are
  permitted to push to the SRS, the shared secret, the chosen vendor
  (cisco_cube / avaya_sbce / metaswitch), and the consent attestation
  flag. Stored on ``Integration.provider_config`` so we don't grow a
  bespoke per-vendor table.

The actual audio frames go through ``SiprecBridge.handle_audio``;
this file is the thin glue between FastAPI and the bridge plus the
admin CRUD. No SBC-side code lives here — that's in the FreeSWITCH
sidecar (``services/telephony/siprec_srs``).
"""

from __future__ import annotations

import base64
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import AuthPrincipal, require_role
from backend.app.db import get_db
from backend.app.models import Integration, SiprecSession
from backend.app.services.audio import AudioFormat
from backend.app.services.telephony.siprec import (
    SIPREC_PROVIDERS,
    SiprecAudioFrame,
    get_bridge,
)
from backend.app.services.token_crypto import decrypt_token, encrypt_token
from backend.app.tenant_ctx import bind_tenant_async

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/siprec", tags=["siprec"])


# ── Pydantic schemas ────────────────────────────────────────────────────


class SiprecEventIn(BaseModel):
    """Inbound event from the SRS sidecar.

    The SRS posts one of these per lifecycle transition or per audio
    chunk. Audio chunks are base64-encoded (the SRS is JSON-only on
    its event channel — bulk audio could go on a binary socket later
    if throughput demands it, but JSON is fine for the inaugural
    deployments).
    """

    event: str = Field(
        ...,
        description=(
            "One of: recording.started | recording.stopped | audio.frame"
        ),
    )
    recording_session_id: str = Field(
        ..., description="rs-metadata <recording session_id>"
    )
    tenant_id: Optional[uuid.UUID] = Field(
        None,
        description=(
            "Resolved tenant — the SRS resolves it from the SBC source "
            "IP via the Integration.provider_config allowlist before "
            "forwarding the event."
        ),
    )
    provider: Optional[str] = Field(
        None,
        description=(
            "siprec_cisco_cube | siprec_avaya_sbce | siprec_metaswitch — "
            "set by the SRS based on the matching Integration row."
        ),
    )
    agent_user_id: Optional[uuid.UUID] = None
    src_call_id: Optional[str] = None
    src_metadata: Dict[str, Any] = Field(default_factory=dict)
    sdp_crypto_suite: Optional[str] = None
    is_consent_attested: bool = False
    end_reason: Optional[str] = None
    # Audio-frame fields (only populated when ``event == "audio.frame"``)
    label: Optional[str] = None
    sequence: Optional[int] = None
    audio_format: Optional[str] = None
    audio_b64: Optional[str] = None

    @field_validator("provider")
    @classmethod
    def _provider_must_be_siprec(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in SIPREC_PROVIDERS:
            raise ValueError(
                f"provider {v!r} is not a SIPREC provider; "
                f"expected one of {SIPREC_PROVIDERS}"
            )
        return v

    @field_validator("event")
    @classmethod
    def _event_known(cls, v: str) -> str:
        allowed = {"recording.started", "recording.stopped", "audio.frame"}
        if v not in allowed:
            raise ValueError(f"unknown event {v!r}; expected one of {sorted(allowed)}")
        return v


class SiprecEventAck(BaseModel):
    ok: bool = True
    recording_session_id: str
    live_session_id: Optional[uuid.UUID] = None
    state: Optional[str] = None  # "started" | "stopped" | "frame_dispatched" | "ignored"


class SiprecAdminConfigIn(BaseModel):
    """Tenant admin payload for ``POST /admin/integrations/siprec``."""

    provider: str = Field(
        ..., description="siprec_cisco_cube | siprec_avaya_sbce | siprec_metaswitch"
    )
    sbc_ip_allowlist: List[str] = Field(
        ...,
        description=(
            "Source IPs (or CIDR blocks) the SRS will accept INVITEs from. "
            "An empty list disables the integration."
        ),
    )
    shared_secret: Optional[str] = Field(
        None,
        description=(
            "Token the SRS sends back on /siprec/events. Generated "
            "server-side when omitted; never returned in plaintext after "
            "creation (only the prefix)."
        ),
    )
    srtp_profile: str = Field(
        "sdes",
        description="sdes | dtls | none — see services/telephony/siprec/srtp.py",
    )
    consent_attestation: bool = Field(
        False,
        description=(
            "Tenant attests that they have legal authority to record the "
            "calls forked by these SBCs. Required True for production."
        ),
    )
    notes: Optional[str] = Field(None, max_length=2000)

    @field_validator("provider")
    @classmethod
    def _provider_in_set(cls, v: str) -> str:
        if v not in SIPREC_PROVIDERS:
            raise ValueError(
                f"provider must be one of {SIPREC_PROVIDERS}; got {v!r}"
            )
        return v

    @field_validator("srtp_profile")
    @classmethod
    def _profile_valid(cls, v: str) -> str:
        if v not in {"sdes", "dtls", "none"}:
            raise ValueError("srtp_profile must be one of sdes | dtls | none")
        return v


class SiprecAdminConfigOut(BaseModel):
    integration_id: uuid.UUID
    provider: str
    sbc_ip_allowlist: List[str]
    srtp_profile: str
    consent_attestation: bool
    shared_secret_prefix: Optional[str]  # first 6 chars only
    created_at: datetime
    notes: Optional[str] = None


# ── /siprec/events ──────────────────────────────────────────────────────


async def _verify_srs_secret(
    recording_session_id: str,
    tenant_id: Optional[uuid.UUID],
    x_srs_token: Optional[str],
    db: AsyncSession,
) -> Integration:
    """Validate the X-SRS-Token header against the tenant's stored secret.

    The token is compared against ``Integration.access_token`` (which
    holds the tenant's encrypted shared secret), using ``hmac.compare_digest``
    to keep the comparison constant-time. On mismatch we raise a 401
    rather than 403 — the SRS distinguishes them: a 401 triggers a
    secret-rotation alert, a 403 is a permanent block.
    """

    if not x_srs_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-SRS-Token header is required",
        )
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id is required on SRS events",
        )

    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider.in_(SIPREC_PROVIDERS),
        )
        .order_by(Integration.created_at.desc())
        .limit(1)
    )
    integ = (await db.execute(stmt)).scalar_one_or_none()
    if integ is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No SIPREC integration configured for tenant {tenant_id}",
        )

    expected = decrypt_token(integ.access_token) or ""
    if not expected or not hmac.compare_digest(expected, x_srs_token):
        # Don't echo the rec session id in the log message because
        # this is the unauthenticated path — log it under DEBUG only.
        logger.warning(
            "SIPREC SRS token mismatch for tenant=%s integration=%s",
            tenant_id,
            integ.id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-SRS-Token does not match",
        )
    logger.debug(
        "SIPREC event authorized: tenant=%s rec=%s",
        tenant_id,
        recording_session_id,
    )
    return integ


@router.post(
    "/events",
    response_model=SiprecEventAck,
    status_code=status.HTTP_200_OK,
)
async def siprec_event(
    payload: SiprecEventIn,
    x_srs_token: Optional[str] = Header(None, alias="X-SRS-Token"),
    db: AsyncSession = Depends(get_db),
) -> SiprecEventAck:
    """Receive lifecycle / audio events from the SRS sidecar.

    Returns 200 with a state token the SRS can use to confirm the
    event was applied. The SRS retries on non-2xx responses, so this
    endpoint is idempotent on ``recording_session_id``.
    """

    # Bind as soon as the payload's tenant_id is known — the Integration
    # read in _verify_srs_secret is bootstrap-readable, but every write
    # downstream (bridge.handle_started/stopped/handle_audio) touches
    # tenant-scoped tables under RLS.
    if payload.tenant_id is not None:
        await bind_tenant_async(db, payload.tenant_id)

    await _verify_srs_secret(
        payload.recording_session_id, payload.tenant_id, x_srs_token, db
    )
    bridge = get_bridge()

    if payload.event == "recording.started":
        if payload.provider is None or payload.tenant_id is None:
            raise HTTPException(
                status_code=400,
                detail="recording.started requires provider + tenant_id",
            )
        state = await bridge.handle_started(
            recording_session_id=payload.recording_session_id,
            tenant_id=payload.tenant_id,
            provider=payload.provider,
            agent_user_id=payload.agent_user_id,
            src_call_id=payload.src_call_id,
            src_metadata=payload.src_metadata,
            is_consent_attested=payload.is_consent_attested,
            sdp_crypto_suite=payload.sdp_crypto_suite,
        )
        return SiprecEventAck(
            recording_session_id=payload.recording_session_id,
            live_session_id=state.live_session_id,
            state="started",
        )

    if payload.event == "recording.stopped":
        await bridge.handle_stopped(
            recording_session_id=payload.recording_session_id,
            reason=payload.end_reason,
        )
        return SiprecEventAck(
            recording_session_id=payload.recording_session_id,
            state="stopped",
        )

    # event == "audio.frame"
    if (
        payload.label is None
        or payload.sequence is None
        or payload.audio_format is None
        or payload.audio_b64 is None
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "audio.frame requires label, sequence, audio_format, audio_b64"
            ),
        )
    try:
        fmt = AudioFormat(payload.audio_format.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"unknown audio_format {payload.audio_format!r}",
        )
    try:
        raw = base64.b64decode(payload.audio_b64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"audio_b64 not valid: {exc}")

    delivered = await bridge.handle_audio(
        SiprecAudioFrame(
            recording_session_id=payload.recording_session_id,
            label=payload.label,
            sequence=payload.sequence,
            audio_format=fmt,
            payload=raw,
        )
    )
    return SiprecEventAck(
        recording_session_id=payload.recording_session_id,
        state="frame_dispatched" if delivered else "ignored",
    )


# ── /admin/integrations/siprec ──────────────────────────────────────────


admin_router = APIRouter(prefix="/admin/integrations/siprec", tags=["siprec-admin"])


def _config_from_integration(integ: Integration) -> SiprecAdminConfigOut:
    cfg = integ.provider_config or {}
    secret_prefix: Optional[str] = None
    if integ.access_token:
        plain = decrypt_token(integ.access_token) or ""
        secret_prefix = plain[:6] + "…" if plain else None
    return SiprecAdminConfigOut(
        integration_id=integ.id,
        provider=integ.provider,
        sbc_ip_allowlist=list(cfg.get("sbc_ip_allowlist", []) or []),
        srtp_profile=str(cfg.get("srtp_profile", "sdes")),
        consent_attestation=bool(cfg.get("consent_attestation", False)),
        shared_secret_prefix=secret_prefix,
        created_at=integ.created_at,
        notes=cfg.get("notes"),
    )


@admin_router.post("", response_model=SiprecAdminConfigOut)
async def upsert_siprec_config(
    body: SiprecAdminConfigIn,
    principal: AuthPrincipal = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> SiprecAdminConfigOut:
    """Create or update the SIPREC integration config for the tenant.

    One Integration row per (tenant, provider). Re-posting with the
    same provider replaces the allowlist + srtp_profile + secret;
    omitting ``shared_secret`` rotates the existing secret to a new
    randomly-generated value (and the response shows only the new
    prefix — store the full value out-of-band when you create it).
    """

    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == principal.tenant.id,
            Integration.provider == body.provider,
        )
        .limit(1)
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    secret_plain = body.shared_secret or secrets.token_urlsafe(32)
    encrypted = encrypt_token(secret_plain)

    cfg = {
        "sbc_ip_allowlist": list(body.sbc_ip_allowlist),
        "srtp_profile": body.srtp_profile,
        "consent_attestation": body.consent_attestation,
        "notes": body.notes,
    }

    if existing is None:
        integ = Integration(
            tenant_id=principal.tenant.id,
            user_id=principal.user_id,
            provider=body.provider,
            access_token=encrypted,
            scopes=[],
            provider_config=cfg,
        )
        db.add(integ)
    else:
        existing.access_token = encrypted
        existing.provider_config = cfg
        existing.user_id = principal.user_id
        integ = existing

    await db.commit()
    await db.refresh(integ)
    return _config_from_integration(integ)


@admin_router.get("", response_model=List[SiprecAdminConfigOut])
async def list_siprec_configs(
    principal: AuthPrincipal = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> List[SiprecAdminConfigOut]:
    """List the SIPREC integrations on this tenant (one per provider)."""

    stmt = (
        select(Integration)
        .where(
            Integration.tenant_id == principal.tenant.id,
            Integration.provider.in_(SIPREC_PROVIDERS),
        )
        .order_by(Integration.created_at.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_config_from_integration(i) for i in rows]


@admin_router.get("/sessions", response_model=List[Dict[str, Any]])
async def list_recent_sessions(
    limit: int = 50,
    principal: AuthPrincipal = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> List[Dict[str, Any]]:
    """Recent SIPREC sessions for the tenant — ops triage view."""

    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    stmt = (
        select(SiprecSession)
        .where(SiprecSession.tenant_id == principal.tenant.id)
        .order_by(SiprecSession.started_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": str(r.id),
                "live_session_id": str(r.live_session_id) if r.live_session_id else None,
                "provider": r.provider,
                "src_session_id": r.src_session_id,
                "src_call_id": r.src_call_id,
                "sdp_crypto_suite": r.sdp_crypto_suite,
                "is_consent_attested": r.is_consent_attested,
                "started_at": r.started_at.replace(tzinfo=timezone.utc).isoformat()
                if r.started_at and r.started_at.tzinfo is None
                else (r.started_at.isoformat() if r.started_at else None),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "end_reason": r.end_reason,
            }
        )
    return out


# Public symbol the main.py marker line includes — keeps the
# include_router call short.
siprec_router = APIRouter()
siprec_router.include_router(router)
siprec_router.include_router(admin_router)
