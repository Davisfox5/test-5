"""WebSocket handlers for live transcription and manager monitoring."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from deepgram import DeepgramClient
import redis.asyncio as aioredis
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.db import async_session
from backend.app.models import (
    Contact,
    Customer,
    Interaction,
    KBChunk,
    KBDocument,
    LiveSession,
    PinnedKBCard,
    Tenant,
)
from backend.app.services.kb.classifier import classify
from backend.app.services.kb.retrieval import RetrievalService, hit_to_payload
from backend.app.services.live_coaching import LiveCoachingService

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def _word_count(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------------
# /ws/live/{session_id}  —  Agent live call connection
# ---------------------------------------------------------------------------

@router.websocket("/ws/live/{session_id}")
async def live_transcription(websocket: WebSocket, session_id: str):
    """Agent connects here to stream audio and receive transcripts + coaching."""

    await websocket.accept()
    logger.info("Agent WebSocket connected for session %s", session_id)

    settings = get_settings()
    redis: Optional[aioredis.Redis] = None
    dg_connection = None
    coaching_service = LiveCoachingService()
    retrieval_service = RetrievalService()

    # Tracking state for coaching triggers.
    last_coaching_time: float = time.time()
    words_since_coaching: int = 0

    # Tenant + contact context for tenant-scoped retrieval + pin-aware exclusions.
    (
        tenant_id,
        contact_id,
        question_keyterms,
        tenant_context,
        customer_brief,
    ) = await _resolve_session_context(session_id)

    # Rehydrate pinned KB cards for this contact so the agent sees them
    # immediately when the call connects.
    if contact_id is not None and tenant_id is not None:
        pins = await _load_pinned_cards(tenant_id, contact_id)
        for pin_payload in pins:
            await _safe_send(websocket, pin_payload)

    try:
        redis = _get_redis()

        # ── Deepgram live transcription setup ────────────────────
        dg_client = DeepgramClient(settings.DEEPGRAM_API_KEY)

        dg_options = {
            "model": "nova-3",
            "diarize": True,
            "interim_results": True,
            "utterance_end_ms": "1000",
        }

        # Tenant-opt-in: only send keyterms when the tenant has configured them.
        # Deepgram bills keyterm prompting as a ~$0.0013/min add-on, so we
        # leave it off for tenants that haven't opted in. Local substring
        # detection inside _contains_keyterm still runs for free either way.
        if question_keyterms:
            dg_options["keyterm"] = question_keyterms

        dg_connection = dg_client.listen.live.v("1")

        # ── Deepgram event handlers ─────────────────────────────

        async def on_transcript(
            _self: Any, result: Any, **kwargs: Any
        ) -> None:
            """Handle incoming Deepgram transcript events."""
            nonlocal last_coaching_time, words_since_coaching

            alt = result.channel.alternatives[0] if result.channel.alternatives else None
            if alt is None or not alt.transcript:
                return

            text = alt.transcript
            speaker = None
            if alt.words:
                speaker = alt.words[0].speaker

            ts = time.time()

            if result.is_final:
                payload = {
                    "type": "final",
                    "text": text,
                    "speaker": speaker,
                    "timestamp": ts,
                }
                await _safe_send(websocket, payload)

                # Publish to monitor channel so managers can follow along.
                if redis is not None:
                    await redis.publish(
                        f"live:{session_id}:events",
                        json.dumps(payload),
                    )

                # Append to session buffer in Redis.
                segment = json.dumps({
                    "text": text,
                    "speaker": speaker,
                    "timestamp": ts,
                })
                if redis is not None:
                    await redis.rpush(f"live:{session_id}:buffer", segment)

                words_since_coaching += _word_count(text)

                # ── Event-driven retrieval (caller turns only) ───
                # Deepgram tags the first speaker as 0; we treat non-zero as caller.
                # Some integrations map speakers differently — if the session
                # has no diarization, speaker will be None and we skip to avoid
                # firing on agent speech.
                if speaker not in (None, 0, "0"):
                    asyncio.create_task(
                        _run_kb_lookup(
                            websocket=websocket,
                            redis=redis,
                            session_id=session_id,
                            retrieval_service=retrieval_service,
                            tenant_id=tenant_id,
                            contact_id=contact_id,
                            caller_text=text,
                            keyterm_hit=_contains_keyterm(text, question_keyterms),
                        )
                    )

                # ── Coaching trigger (30s heartbeat) ─────────────
                elapsed = ts - last_coaching_time
                if elapsed >= 30 or words_since_coaching >= 200:
                    await _run_coaching(
                        websocket,
                        redis,
                        session_id,
                        coaching_service,
                        retrieval_service,
                        tenant_id,
                        contact_id,
                        tenant_context,
                        customer_brief,
                    )
                    last_coaching_time = time.time()
                    words_since_coaching = 0
            else:
                # Interim / partial result.
                payload = {
                    "type": "partial",
                    "text": text,
                    "speaker": speaker,
                    "timestamp": ts,
                }
                await _safe_send(websocket, payload)

        async def on_utterance_end(
            _self: Any, result: Any, **kwargs: Any
        ) -> None:
            """Handle Deepgram utterance-end marker."""
            nonlocal last_coaching_time, words_since_coaching

            # Utterance end can also trigger coaching if thresholds met.
            ts = time.time()
            elapsed = ts - last_coaching_time
            if elapsed >= 30 or words_since_coaching >= 200:
                await _run_coaching(
                    websocket,
                    redis,
                    session_id,
                    coaching_service,
                    retrieval_service,
                    tenant_id,
                    contact_id,
                    tenant_context,
                )
                last_coaching_time = time.time()
                words_since_coaching = 0

        async def on_error(
            _self: Any, error: Any, **kwargs: Any
        ) -> None:
            logger.error("Deepgram error for session %s: %s", session_id, error)

        dg_connection.on("Results", on_transcript)
        dg_connection.on("UtteranceEnd", on_utterance_end)
        dg_connection.on("Error", on_error)

        # Start the Deepgram connection.
        await dg_connection.start(dg_options)

        # ── Main receive loop ────────────────────────────────────
        while True:
            data = await websocket.receive()

            if "bytes" in data:
                # Binary audio chunk — forward to Deepgram.
                await dg_connection.send(data["bytes"])
            elif "text" in data:
                # Text message from agent (e.g., control commands).
                try:
                    msg = json.loads(data["text"])
                    msg_type = msg.get("type")
                    if msg_type == "ping":
                        await _safe_send(websocket, {"type": "pong"})
                except json.JSONDecodeError:
                    pass

    except WebSocketDisconnect:
        logger.info("Agent WebSocket disconnected for session %s", session_id)
    except Exception:
        logger.exception("Error in live transcription for session %s", session_id)
    finally:
        # ── Cleanup ──────────────────────────────────────────────
        if dg_connection is not None:
            try:
                await dg_connection.finish()
            except Exception:
                logger.exception("Error closing Deepgram connection for %s", session_id)

        # Dispatch batch analysis from the accumulated buffer.
        if redis is not None:
            try:
                await _dispatch_batch_analysis(redis, session_id)
            except Exception:
                logger.exception("Error dispatching batch analysis for %s", session_id)
            await redis.aclose()

        logger.info("Cleanup complete for session %s", session_id)


# ---------------------------------------------------------------------------
# /ws/monitor/{session_id}  —  Manager monitoring connection
# ---------------------------------------------------------------------------

@router.websocket("/ws/monitor/{session_id}")
async def monitor_session(websocket: WebSocket, session_id: str):
    """Manager connects here to observe a live session and send whispers."""

    await websocket.accept()
    logger.info("Monitor WebSocket connected for session %s", session_id)

    redis: Optional[aioredis.Redis] = None
    pubsub: Optional[aioredis.client.PubSub] = None

    try:
        redis = _get_redis()
        pubsub = redis.pubsub()
        channel = f"live:{session_id}:events"
        await pubsub.subscribe(channel)

        async def _relay_events() -> None:
            """Read events from Redis pub/sub and forward to manager WebSocket."""
            assert pubsub is not None
            async for message in pubsub.listen():
                if message["type"] == "message":
                    # Forward raw JSON event to the manager.
                    await _safe_send_raw(websocket, message["data"])

        async def _receive_whispers() -> None:
            """Receive whisper messages from manager and publish to Redis."""
            assert redis is not None
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "whisper":
                        whisper_event = json.dumps({
                            "type": "whisper",
                            "from_user_id": msg.get("from_user_id", "manager"),
                            "message": msg.get("message", ""),
                        })
                        # Publish so the agent's live connection can pick it up.
                        await redis.publish(channel, whisper_event)
                except json.JSONDecodeError:
                    pass

        # Run both tasks concurrently; if either ends we tear down.
        relay_task = asyncio.create_task(_relay_events())
        whisper_task = asyncio.create_task(_receive_whispers())

        done, pending = await asyncio.wait(
            [relay_task, whisper_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    except WebSocketDisconnect:
        logger.info("Monitor WebSocket disconnected for session %s", session_id)
    except Exception:
        logger.exception("Error in monitor WebSocket for session %s", session_id)
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:
                pass
        if redis is not None:
            await redis.aclose()

        logger.info("Monitor cleanup complete for session %s", session_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _safe_send(ws: WebSocket, payload: dict) -> None:
    """Send JSON to a WebSocket, swallowing errors if the connection closed."""
    try:
        await ws.send_json(payload)
    except Exception:
        logger.debug("Failed to send to WebSocket (likely closed)")


async def _safe_send_raw(ws: WebSocket, raw: str) -> None:
    """Send a raw string to a WebSocket, swallowing errors."""
    try:
        await ws.send_text(raw)
    except Exception:
        logger.debug("Failed to send raw text to WebSocket (likely closed)")


async def _run_coaching(
    websocket: WebSocket,
    redis: Optional[aioredis.Redis],
    session_id: str,
    coaching_service: LiveCoachingService,
    retrieval_service: Optional[RetrievalService] = None,
    tenant_id: Optional[uuid.UUID] = None,
    contact_id: Optional[uuid.UUID] = None,
    tenant_context: Optional[Dict[str, Any]] = None,
    customer_brief: Optional[Dict[str, Any]] = None,
) -> None:
    """Fetch recent buffer, load coaching state, run incremental coaching.

    When a retrieval service and tenant are available, we also grab the most
    recent caller turn and fetch top-K KB chunks as ``kb_hits`` so Haiku can
    weave verified documentation into the phrasing.
    """
    if redis is None:
        return

    try:
        # Load the recent transcript segments from the buffer.
        raw_segments = await redis.lrange(f"live:{session_id}:buffer", 0, -1)
        segments: List[dict] = []
        for raw in raw_segments:
            try:
                segments.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

        if not segments:
            return

        # Load previous coaching state.
        state_raw = await redis.get(f"live:{session_id}:coaching_state")
        previous_state: dict = {}
        if state_raw:
            try:
                previous_state = json.loads(state_raw)
            except json.JSONDecodeError:
                previous_state = {}

        # Use only the most recent segments as "new" (last 20 segments or so).
        new_segments = segments[-20:]

        # Optionally enrich with KB hits drawn from the most recent caller turn.
        kb_hits_for_coach: List[dict] = []
        if retrieval_service is not None and tenant_id is not None:
            caller_turn = _last_caller_text(new_segments)
            if caller_turn:
                kb_hits_for_coach = await _fetch_kb_hits_for_coaching(
                    retrieval_service, tenant_id, contact_id, caller_turn
                )

        result = await coaching_service.hint_incremental(
            new_segments=new_segments,
            previous_state=previous_state,
            kb_hits=kb_hits_for_coach or None,
            tenant_context=tenant_context,
            customer_brief=customer_brief,
        )

        # Send each hint to the agent.
        for hint_obj in result.get("hints", []):
            coaching_payload = {
                "type": "coaching",
                "hint": hint_obj.get("hint", ""),
                "source_doc_title": hint_obj.get("source_doc_title"),
                "confidence": hint_obj.get("confidence", 0.0),
            }
            await _safe_send(websocket, coaching_payload)

            # Also publish to monitor channel.
            await redis.publish(
                f"live:{session_id}:events",
                json.dumps(coaching_payload),
            )

        # Persist updated coaching state.
        updated_state = result.get("updated_state", previous_state)
        await redis.set(
            f"live:{session_id}:coaching_state",
            json.dumps(updated_state, default=str),
        )

    except Exception:
        logger.exception("Coaching failed for session %s", session_id)


async def _dispatch_batch_analysis(
    redis: aioredis.Redis,
    session_id: str,
) -> None:
    """Finalise a live session — persist Interaction, enqueue batch pipeline.

    Steps:
    1. Read the transcript buffer from Redis.
    2. Convert wall-clock timestamps into pipeline-ready segments
       (``start`` / ``end`` / ``speaker_id`` / ``text`` / ``confidence``).
    3. If the LiveSession already points at an Interaction, update it;
       otherwise create a new Interaction row on the session's tenant.
    4. Mark the LiveSession completed and enqueue ``process_voice_interaction``.
       The existing task detects the pre-populated transcript and skips the
       audio-transcribe step, then runs triage → analysis → outcome inference
       → scorecards → customer-brief rebuild.
    5. Clean up Redis keys either way.

    Tolerant of missing rows — development setups without a LiveSession row
    (or with an invalid session id) just clean up Redis and return.
    """
    buffer_keys = (
        f"live:{session_id}:buffer",
        f"live:{session_id}:coaching_state",
        f"live:{session_id}:recent_kb_chunk_ids",
    )

    raw_segments = await redis.lrange(f"live:{session_id}:buffer", 0, -1)
    if not raw_segments:
        logger.info("No buffer data for session %s, skipping batch analysis", session_id)
        await redis.delete(*buffer_keys)
        return

    segments_raw: List[dict] = []
    for raw in raw_segments:
        try:
            segments_raw.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    if not segments_raw:
        await redis.delete(*buffer_keys)
        return

    segments_dicts = _buffer_to_pipeline_segments(segments_raw)
    full_text = " ".join(s.get("text", "") for s in segments_dicts)
    logger.info(
        "Session %s ended with %d segments (%d words); dispatching batch analysis",
        session_id,
        len(segments_dicts),
        _word_count(full_text),
    )

    interaction_id: Optional[uuid.UUID] = None

    try:
        sess_uuid = uuid.UUID(session_id)
    except ValueError:
        logger.info("Session id %s is not a UUID; skipping DB finalize", session_id)
        await redis.delete(*buffer_keys)
        return

    try:
        async with async_session() as db:
            sess_row = await db.get(LiveSession, sess_uuid)
            if sess_row is None:
                logger.info("LiveSession %s not found; Redis cleanup only", session_id)
                return

            first_ts = segments_raw[0].get("timestamp")
            last_ts = segments_raw[-1].get("timestamp")
            duration = (
                int(last_ts - first_ts)
                if isinstance(first_ts, (int, float)) and isinstance(last_ts, (int, float))
                else None
            )

            interaction: Optional[Interaction] = None
            if sess_row.interaction_id is not None:
                interaction = await db.get(Interaction, sess_row.interaction_id)

            if interaction is None:
                interaction = Interaction(
                    tenant_id=sess_row.tenant_id,
                    agent_id=sess_row.agent_id,
                    channel="voice",
                    source=sess_row.source or "live",
                    status="processing",
                    engine="deepgram",
                    transcript=segments_dicts,
                    duration_seconds=duration,
                )
                db.add(interaction)
                await db.flush()
                sess_row.interaction_id = interaction.id
            else:
                # Don't clobber a richer transcript from a previous run —
                # only set ours when the row is still empty.
                if not interaction.transcript:
                    interaction.transcript = segments_dicts
                if interaction.duration_seconds is None:
                    interaction.duration_seconds = duration
                interaction.status = "processing"

            sess_row.status = "completed"
            sess_row.ended_at = datetime.now(timezone.utc)
            sess_row.transcript_buffer = segments_dicts

            interaction_id = interaction.id
    except Exception:
        logger.exception("Failed to finalize live session %s", session_id)
        interaction_id = None
    finally:
        # Always clean up Redis even on DB errors; the live buffer isn't
        # the source of truth any more.
        await redis.delete(*buffer_keys)

    if interaction_id is None:
        return

    # Enqueue the existing voice pipeline. It detects the pre-populated
    # transcript and skips audio download + transcription, going straight
    # to PII redaction → metrics → triage → AI analysis → outcome inference
    # → customer-brief rebuild.
    try:
        from backend.app.tasks import process_voice_interaction

        process_voice_interaction.delay(str(interaction_id))
    except Exception:
        # Best-effort: if Celery isn't available in this process, log but
        # don't crash the websocket cleanup path.
        logger.exception(
            "Failed to enqueue process_voice_interaction for %s — "
            "interaction row is saved; operator can retry manually",
            interaction_id,
        )


def _buffer_to_pipeline_segments(segments_raw: List[dict]) -> List[dict]:
    """Convert live Redis segments to the shape ``_run_pipeline`` expects.

    Live buffer entries have ``{text, speaker, timestamp}`` with a wall-clock
    timestamp. The pipeline wants relative ``start``/``end`` seconds plus
    ``speaker_id``/``confidence``.
    """
    out: List[dict] = []
    if not segments_raw:
        return out

    base_ts = None
    for s in segments_raw:
        ts = s.get("timestamp")
        if isinstance(ts, (int, float)):
            base_ts = ts
            break
    if base_ts is None:
        base_ts = 0.0

    for i, s in enumerate(segments_raw):
        ts = s.get("timestamp")
        start = float(ts - base_ts) if isinstance(ts, (int, float)) else float(i * 2)
        # Estimate end as the next segment's start, or +2s for the last one.
        end = start + 2.0
        if i + 1 < len(segments_raw):
            nxt = segments_raw[i + 1].get("timestamp")
            if isinstance(nxt, (int, float)):
                end = float(nxt - base_ts)
        speaker = s.get("speaker")
        out.append(
            {
                "start": start,
                "end": end,
                "text": s.get("text", ""),
                "speaker_id": str(speaker) if speaker is not None else "Unknown",
                "confidence": 1.0,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Live KB retrieval
# ---------------------------------------------------------------------------


def _contains_keyterm(text: str, keyterms: List[str]) -> bool:
    """Case-insensitive substring match against tenant keyterms."""
    if not keyterms or not text:
        return False
    lower = text.lower()
    return any(k.lower() in lower for k in keyterms)


def _last_caller_text(segments: List[dict]) -> str:
    """Return the most recent caller turn text (speaker != 0)."""
    for seg in reversed(segments):
        spk = seg.get("speaker")
        if spk not in (None, 0, "0"):
            return seg.get("text", "") or ""
    return ""


async def _resolve_session_context(
    session_id: str,
) -> tuple[
    Optional[uuid.UUID],
    Optional[uuid.UUID],
    List[str],
    Dict[str, Any],
    Dict[str, Any],
]:
    """Load session-time context: tenant_id, contact_id, keyterms, tenant
    brief, and customer brief (if a contact is linked to a customer).

    Tolerates unknown session ids (returns empty defaults) so development
    setups that skip the LiveSession row still work — retrieval and context
    injection are just disabled.
    """
    empty = (None, None, [], {}, {})
    try:
        uuid.UUID(session_id)
    except ValueError:
        return empty

    try:
        async with async_session() as db:
            stmt = (
                select(LiveSession, Interaction, Tenant)
                .join(Tenant, Tenant.id == LiveSession.tenant_id)
                .join(
                    Interaction,
                    Interaction.id == LiveSession.interaction_id,
                    isouter=True,
                )
                .where(LiveSession.id == uuid.UUID(session_id))
            )
            row = (await db.execute(stmt)).first()
            if row is None:
                return empty
            sess, interaction, tenant = row
            contact_id = interaction.contact_id if interaction else None
            keyterms = list(tenant.question_keyterms or [])
            ctx = dict(tenant.tenant_context or {})

            customer_brief: Dict[str, Any] = {}
            if contact_id is not None:
                contact_row = await db.get(Contact, contact_id)
                if contact_row is not None and contact_row.customer_id is not None:
                    customer = await db.get(Customer, contact_row.customer_id)
                    if customer is not None:
                        customer_brief = dict(customer.customer_brief or {})

            return sess.tenant_id, contact_id, keyterms, ctx, customer_brief
    except Exception:
        logger.exception("Failed to resolve session context for %s", session_id)
        return empty


async def _load_pinned_cards(
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> List[dict]:
    """Return rehydrated pinned-card payloads for the agent UI."""
    try:
        async with async_session() as db:
            stmt = (
                select(PinnedKBCard, KBChunk, KBDocument)
                .join(KBChunk, KBChunk.id == PinnedKBCard.chunk_id)
                .join(KBDocument, KBDocument.id == PinnedKBCard.doc_id)
                .where(
                    PinnedKBCard.tenant_id == tenant_id,
                    PinnedKBCard.contact_id == contact_id,
                )
                .order_by(PinnedKBCard.pinned_at.desc())
            )
            rows = (await db.execute(stmt)).all()
    except Exception:
        logger.exception("Failed to load pinned KB cards")
        return []

    payloads: List[dict] = []
    for pin, chunk, doc in rows:
        payloads.append(
            {
                "type": "kb_answer",
                "pinned": True,
                "pin_id": str(pin.id),
                "query": "",
                "snippet": chunk.text,
                "chunk_id": str(chunk.id),
                "doc_id": str(doc.id),
                "doc_title": doc.title,
                "source_url": doc.source_url,
                "confidence": 1.0,
                "source": "pin_rehydrate",
            }
        )
    return payloads


async def _excluded_chunk_ids(
    redis: Optional[aioredis.Redis],
    session_id: str,
    tenant_id: Optional[uuid.UUID],
    contact_id: Optional[uuid.UUID],
) -> List[uuid.UUID]:
    """Chunks we've already surfaced this session + contact pins."""
    excluded: List[uuid.UUID] = []

    if redis is not None:
        recent = await redis.lrange(f"live:{session_id}:recent_kb_chunk_ids", 0, -1)
        for raw in recent:
            try:
                excluded.append(uuid.UUID(raw))
            except (ValueError, TypeError):
                continue

    if tenant_id is not None and contact_id is not None:
        try:
            async with async_session() as db:
                rows = await db.execute(
                    select(PinnedKBCard.chunk_id).where(
                        PinnedKBCard.tenant_id == tenant_id,
                        PinnedKBCard.contact_id == contact_id,
                    )
                )
                for (chunk_id,) in rows.all():
                    excluded.append(uuid.UUID(str(chunk_id)))
        except Exception:
            logger.exception("Failed to load pinned chunk ids")

    return excluded


async def _fetch_kb_hits_for_coaching(
    retrieval_service: RetrievalService,
    tenant_id: uuid.UUID,
    contact_id: Optional[uuid.UUID],
    caller_text: str,
) -> List[dict]:
    """Fetch top-K hits to feed into the coaching LLM as ``kb_hits``."""
    try:
        async with async_session() as db:
            excluded: List[uuid.UUID] = []
            if contact_id is not None:
                pins = await retrieval_service.pinned_chunk_ids(db, tenant_id, contact_id)
                excluded = pins
            hits = await retrieval_service.search(
                db,
                tenant_id=tenant_id,
                query=caller_text,
                k=3,
                exclude_chunk_ids=excluded,
            )
            return [
                {
                    "title": h.doc_title or "Untitled",
                    "snippet": h.text[:400],
                    "source_url": h.source_url,
                    "score": h.score,
                }
                for h in hits
            ]
    except Exception:
        logger.exception("KB hits fetch failed for coaching")
        return []


async def _run_kb_lookup(
    websocket: WebSocket,
    redis: Optional[aioredis.Redis],
    session_id: str,
    retrieval_service: RetrievalService,
    tenant_id: Optional[uuid.UUID],
    contact_id: Optional[uuid.UUID],
    caller_text: str,
    keyterm_hit: bool,
) -> None:
    """Classify the caller turn and, if it's a question, push KB cards.

    Sends one ``kb_answer`` message per hit to the agent and the monitor
    channel. Non-blocking — called via ``asyncio.create_task``.
    """
    if tenant_id is None or not caller_text.strip():
        return

    try:
        verdict = await classify(
            caller_text, deepgram_keyterm_hit=keyterm_hit
        )
        if not verdict.is_question:
            return

        excluded = await _excluded_chunk_ids(redis, session_id, tenant_id, contact_id)

        async with async_session() as db:
            hits = await retrieval_service.search(
                db,
                tenant_id=tenant_id,
                query=verdict.query or caller_text,
                k=3,
                exclude_chunk_ids=excluded,
            )

        if not hits:
            return

        # Per the UX decision: show all results as suggestions regardless of
        # confidence. Sort by score so the strongest hit is on top.
        hits.sort(key=lambda h: h.score, reverse=True)

        for hit in hits:
            payload = {
                "type": "kb_answer",
                "pinned": False,
                "query": verdict.query or caller_text,
                "snippet": hit.text,
                **{
                    k: v
                    for k, v in hit_to_payload(hit).items()
                    if k in ("chunk_id", "doc_id", "doc_title", "source_url", "score")
                },
                "confidence": hit.score,
                "urgency": verdict.urgency,
                "source": verdict.source,
            }
            await _safe_send(websocket, payload)
            if redis is not None:
                await redis.publish(
                    f"live:{session_id}:events",
                    json.dumps(payload),
                )
                # Remember this chunk so we don't re-surface it on the next turn.
                await redis.rpush(
                    f"live:{session_id}:recent_kb_chunk_ids",
                    str(hit.chunk_id),
                )
                await redis.expire(
                    f"live:{session_id}:recent_kb_chunk_ids", 60 * 60
                )
    except Exception:
        logger.exception("KB lookup failed for session %s", session_id)
