"""WebSocket handlers for live transcription and manager monitoring."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from deepgram import DeepgramClient
import redis.asyncio as aioredis

from backend.app.config import get_settings
from backend.app.services.live_coaching import LiveCoachingService
from backend.app.services.live_coaching_features import (
    LFTriggerScanner,
    LiveFeatureWindow,
    LiveTurn,
)
from backend.app.services.ws_tickets import (
    RateLimitedError,
    WebSocketAuthError,
    consume_ticket,
    enforce_new_connection_quota,
)

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
    """Agent connects here to stream audio and receive transcripts + coaching.

    Authentication: the client must first call ``POST /ws/tickets`` with
    its Bearer token to get a single-use ticket bound to ``(tenant_id,
    session_id, role="agent")``, then open this WebSocket with
    ``?ticket=<ticket>``.  The handler validates the ticket *before*
    accepting the connection; an absent, expired, already-consumed, or
    mismatched ticket closes the socket with code 4401.
    """

    settings = get_settings()
    redis: Optional[aioredis.Redis] = None
    dg_connection = None

    # ── Auth handshake ───────────────────────────────────────
    ticket_param = websocket.query_params.get("ticket") if websocket.query_params else None
    auth_redis = _get_redis()
    try:
        try:
            ticket = await consume_ticket(
                auth_redis,
                ticket_param,
                expected_session_id=session_id,
                expected_role="agent",
            )
            await enforce_new_connection_quota(auth_redis, f"tenant:{ticket.tenant_id}")
        except RateLimitedError:
            logger.warning(
                "ws/live rate-limited for session %s (ticket=%s)",
                session_id, bool(ticket_param),
            )
            await websocket.close(code=4429, reason="rate_limited")
            return
        except WebSocketAuthError as exc:
            logger.info(
                "ws/live auth refused for %s: %s", session_id, exc.reason,
            )
            await websocket.close(code=4401, reason="unauthorized")
            return
    finally:
        try:
            await auth_redis.aclose()
        except Exception:  # noqa: BLE001
            pass

    await websocket.accept()
    logger.info(
        "Agent WebSocket connected for session %s (tenant=%s)",
        session_id, ticket.tenant_id,
    )

    coaching_service = LiveCoachingService()

    # Tracking state for coaching triggers.
    last_coaching_time: float = time.time()
    words_since_coaching: int = 0

    # Deterministic-feature state (per-connection, no external deps).
    feature_window = LiveFeatureWindow(window_sec=60.0)
    lf_scanner = LFTriggerScanner(cooldown_sec=30.0)
    # Throttle: snapshot every N finals or every MIN_SEC since last emit.
    last_features_emit_at: float = 0.0
    finals_since_features_emit: int = 0
    FEATURES_MIN_INTERVAL_SEC = 5.0
    FEATURES_EVERY_N_FINALS = 3

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

        # TODO: pull keyterms from tenant config when available
        # if tenant_keyterms:
        #     dg_options["keywords"] = tenant_keyterms

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

                # ── Deterministic live features (zero-LLM path) ───
                # Convention: Deepgram speaker index 0 == agent.  Anything
                # else (incl. None) is treated as customer.  Estimated turn
                # duration is a rough heuristic — Deepgram's final event
                # does not carry per-turn duration.
                word_count = _word_count(text)
                est_duration = max(0.3, word_count * 0.4)
                turn = LiveTurn(
                    speaker_id=str(speaker) if speaker is not None else "customer",
                    text=text,
                    start=ts,
                    end=ts + est_duration,
                    is_agent=(speaker == 0),
                )
                feature_window.push(turn)

                try:
                    alerts = lf_scanner.push(turn, feature_window)
                except Exception:  # noqa: BLE001 — alerts must never kill the loop
                    logger.exception("LF scanner raised for session %s", session_id)
                    alerts = []
                for alert in alerts:
                    alert_payload = {"type": "alert", **alert.to_wire()}
                    await _safe_send(websocket, alert_payload)
                    if redis is not None:
                        await redis.publish(
                            f"live:{session_id}:events",
                            json.dumps(alert_payload),
                        )

                # Features snapshot — throttled so the DOM doesn't churn.
                finals_since_features_emit += 1
                features_elapsed = ts - last_features_emit_at
                if (
                    finals_since_features_emit >= FEATURES_EVERY_N_FINALS
                    and features_elapsed >= FEATURES_MIN_INTERVAL_SEC
                ):
                    snapshot_payload = {
                        "type": "features",
                        **feature_window.snapshot().to_wire(),
                    }
                    await _safe_send(websocket, snapshot_payload)
                    last_features_emit_at = ts
                    finals_since_features_emit = 0

                # ── Coaching trigger ─────────────────────────────
                elapsed = ts - last_coaching_time
                if elapsed >= 30 or words_since_coaching >= 200:
                    await _run_coaching(
                        websocket,
                        redis,
                        session_id,
                        coaching_service,
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
    """Manager connects here to observe a live session and send whispers.

    Authentication: same ticket handshake as ``/ws/live``, but the
    ticket must have been issued for ``role="monitor"``.  The ticket
    endpoint in turn requires the requesting user to hold
    ``manager`` or ``admin``, so by the time we see a monitor ticket
    here it has already been role-checked.
    """

    redis: Optional[aioredis.Redis] = None
    pubsub: Optional[aioredis.client.PubSub] = None

    # ── Auth handshake (monitor role required) ──────────────
    ticket_param = websocket.query_params.get("ticket") if websocket.query_params else None
    auth_redis = _get_redis()
    try:
        try:
            ticket = await consume_ticket(
                auth_redis,
                ticket_param,
                expected_session_id=session_id,
                expected_role="monitor",
            )
            await enforce_new_connection_quota(auth_redis, f"tenant:{ticket.tenant_id}")
        except RateLimitedError:
            await websocket.close(code=4429, reason="rate_limited")
            return
        except WebSocketAuthError as exc:
            logger.info(
                "ws/monitor auth refused for %s: %s", session_id, exc.reason,
            )
            await websocket.close(code=4401, reason="unauthorized")
            return
    finally:
        try:
            await auth_redis.aclose()
        except Exception:  # noqa: BLE001
            pass

    await websocket.accept()
    logger.info(
        "Monitor WebSocket connected for session %s (tenant=%s)",
        session_id, ticket.tenant_id,
    )

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
) -> None:
    """Fetch recent buffer, load coaching state, run incremental coaching."""
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

        result = await coaching_service.hint_incremental(
            new_segments=new_segments,
            previous_state=previous_state,
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
    """Create an interaction record from the buffer and queue batch analysis.

    This is a placeholder — the actual implementation would persist to the DB
    and enqueue a background task (e.g., Celery / ARQ).
    """
    raw_segments = await redis.lrange(f"live:{session_id}:buffer", 0, -1)
    if not raw_segments:
        logger.info("No buffer data for session %s, skipping batch analysis", session_id)
        return

    segments: List[dict] = []
    for raw in raw_segments:
        try:
            segments.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    full_text = " ".join(seg.get("text", "") for seg in segments)
    logger.info(
        "Session %s ended with %d segments (%d words). "
        "Batch analysis dispatch placeholder.",
        session_id,
        len(segments),
        _word_count(full_text),
    )

    # TODO: create Interaction record in DB, upload audio to S3,
    #       enqueue ai_analysis task.

    # Clean up Redis keys for this session.
    await redis.delete(
        f"live:{session_id}:buffer",
        f"live:{session_id}:coaching_state",
    )
