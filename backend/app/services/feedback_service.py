"""Feedback ingestion — Redis stream producer/consumer + diff helpers.

Layer 1 of the continuous-improvement system.  Front-end events arrive at
``POST /api/v1/feedback/batch`` and get pushed to the ``feedback.events``
Redis stream.  A Celery worker drains the stream into the ``feedback_events``
table and fans the events out to enterprise webhooks.

The Redis hop keeps user-facing latency at zero — the API endpoint just
``XADD``s and returns; persistence happens behind the scenes.
"""

from __future__ import annotations

import difflib
import json
import logging
import uuid as _uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.models import FeedbackDailyRollup, FeedbackEvent, Tenant

logger = logging.getLogger(__name__)


STREAM_KEY = "feedback.events"
CONSUMER_GROUP = "feedback-consumers"
CONSUMER_NAME = "worker-1"
DRAFT_CACHE_TTL_SECONDS = 60 * 60  # 1h matches plan


# ── Redis client ─────────────────────────────────────────────────────────


def _redis():
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception:  # pragma: no cover
        logger.exception("Could not connect to Redis for feedback service (non-fatal)")
        return None


# ── Producer ─────────────────────────────────────────────────────────────


def emit_event(
    *,
    tenant_id: Any,
    surface: str,
    event_type: str,
    signal_type: str = "explicit",
    interaction_id: Optional[Any] = None,
    conversation_id: Optional[Any] = None,
    action_item_id: Optional[Any] = None,
    user_id: Optional[Any] = None,
    insight_dimension: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    session_id: Optional[Any] = None,
) -> bool:
    """Push a single event onto the Redis stream.  Best-effort, never raises."""
    r = _redis()
    if r is None:
        return False
    body = {
        "tenant_id": str(tenant_id),
        "surface": surface,
        "event_type": event_type,
        "signal_type": signal_type,
        "interaction_id": str(interaction_id) if interaction_id else "",
        "conversation_id": str(conversation_id) if conversation_id else "",
        "action_item_id": str(action_item_id) if action_item_id else "",
        "user_id": str(user_id) if user_id else "",
        "insight_dimension": insight_dimension or "",
        "session_id": str(session_id) if session_id else "",
        "payload": json.dumps(payload or {}),
        "ts": datetime.utcnow().isoformat(),
    }
    try:
        r.xadd(STREAM_KEY, body, maxlen=100_000, approximate=True)
        return True
    except Exception:
        logger.exception("Failed to xadd feedback event (non-fatal)")
        return False


def emit_events(events: Iterable[Dict[str, Any]]) -> int:
    """Bulk emit; returns the number successfully pushed."""
    count = 0
    for ev in events:
        if emit_event(**ev):
            count += 1
    return count


# ── Consumer (Celery worker) ─────────────────────────────────────────────


def _ensure_group(r) -> None:
    try:
        r.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
    except Exception as exc:
        # BUSYGROUP raised when the group already exists — that's fine.
        if "BUSYGROUP" not in str(exc):
            logger.exception("xgroup_create failed (non-fatal)")


def _row_from_message(fields: Dict[str, str]) -> Dict[str, Any]:
    payload_raw = fields.get("payload") or "{}"
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        payload = {"_unparsed": payload_raw}

    def _maybe_uuid(val: Optional[str]) -> Optional[_uuid.UUID]:
        if not val:
            return None
        try:
            return _uuid.UUID(val)
        except (TypeError, ValueError):
            return None

    return {
        "tenant_id": _maybe_uuid(fields.get("tenant_id")),
        "interaction_id": _maybe_uuid(fields.get("interaction_id")),
        "conversation_id": _maybe_uuid(fields.get("conversation_id")),
        "action_item_id": _maybe_uuid(fields.get("action_item_id")),
        "user_id": _maybe_uuid(fields.get("user_id")),
        "session_id": _maybe_uuid(fields.get("session_id")),
        "surface": fields.get("surface", "analysis"),
        "event_type": fields.get("event_type", "unknown"),
        "signal_type": fields.get("signal_type", "explicit"),
        "insight_dimension": fields.get("insight_dimension") or None,
        "payload": payload,
    }


def consume_batch(session: Session, max_messages: int = 500) -> Dict[str, Any]:
    """Drain up to ``max_messages`` from the stream into ``feedback_events``.

    Called every 30s by Celery Beat.  Idempotent at the message level — once
    we ack we won't process again.  Failed inserts get re-claimed on the next
    poll because they remain pending.
    """
    r = _redis()
    if r is None:
        return {"status": "no_redis", "ingested": 0}

    _ensure_group(r)

    try:
        messages = r.xreadgroup(
            CONSUMER_GROUP,
            CONSUMER_NAME,
            {STREAM_KEY: ">"},
            count=max_messages,
            block=1000,
        )
    except Exception:
        logger.exception("xreadgroup failed")
        return {"status": "error", "ingested": 0}

    ingested = 0
    fanned = 0
    if not messages:
        return {"status": "empty", "ingested": 0}

    ack_ids: List[str] = []
    for _stream, entries in messages:
        for msg_id, fields in entries:
            try:
                row = _row_from_message(fields)
                if row["tenant_id"] is None:
                    # Reject any event without a tenant — would break RLS later.
                    ack_ids.append(msg_id)
                    continue
                fb = FeedbackEvent(**row)
                # RLS: events in one batch span tenants; bind + flush per
                # message so each INSERT's WITH CHECK evaluates under its
                # own tenant (the trailing commit is tenant-agnostic).
                from backend.app.tenant_ctx import tenant_context

                with tenant_context(row["tenant_id"], session):
                    session.add(fb)
                    session.flush()
                    # Fan out to enterprise webhooks (best-effort) — reads
                    # the tenant's Webhook rows, so it needs the context.
                    fanned += _dispatch_webhook(session, row)
                ingested += 1
                ack_ids.append(msg_id)
                # Bump Prometheus counter (no-op if prometheus_client missing).
                try:
                    from backend.app.services import metrics as _metrics

                    _metrics.FEEDBACK_EVENTS.labels(
                        tenant=str(row["tenant_id"]),
                        surface=row["surface"],
                        event_type=row["event_type"],
                    ).inc()
                    if row["event_type"] in (
                        "reply_sent_unchanged",
                        "reply_edited_before_send",
                    ):
                        sim = (row["payload"] or {}).get("similarity")
                        if isinstance(sim, (int, float)):
                            _metrics.REPLY_EDIT_DISTANCE.labels(
                                tenant=str(row["tenant_id"]),
                                variant_id=str(row["payload"].get("variant_id") or "unknown"),
                            ).observe(1.0 - float(sim))
                    if row["event_type"] == "classification_overridden":
                        _metrics.CLASSIFICATION_OVERRIDE.labels(
                            tenant=str(row["tenant_id"])
                        ).inc()
                except Exception:
                    pass
            except Exception:
                logger.exception("Failed to persist feedback event %s", msg_id)
                # Don't ack — let the next poll re-deliver.
    if ingested:
        try:
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Feedback batch commit failed")
            return {"status": "commit_error", "ingested": 0}
    if ack_ids:
        try:
            r.xack(STREAM_KEY, CONSUMER_GROUP, *ack_ids)
        except Exception:
            logger.exception("xack failed (non-fatal)")
    return {"status": "ok", "ingested": ingested, "fanned": fanned}


def _dispatch_webhook(session: Session, row: Dict[str, Any]) -> int:
    """Fire a ``feedback.event`` webhook to any tenants subscribed to it."""
    try:
        from backend.app.services.webhook_dispatcher import dispatch_sync

        payload = {
            "event": "feedback.event",
            "tenant_id": str(row["tenant_id"]),
            "surface": row["surface"],
            "event_type": row["event_type"],
            "interaction_id": str(row["interaction_id"]) if row["interaction_id"] else None,
            "conversation_id": str(row["conversation_id"]) if row["conversation_id"] else None,
            "payload": row["payload"],
        }
        dispatch_sync(session, row["tenant_id"], "feedback.event", payload)
        return 1
    except Exception:
        logger.exception("Webhook dispatch from feedback consumer failed (non-fatal)")
        return 0


# ── Reply draft cache (for edit-distance signal) ─────────────────────────


def cache_draft_body(user_id: Any, conversation_id: Any, body: str) -> None:
    """Store the AI-drafted body so we can diff against the actually-sent body."""
    r = _redis()
    if r is None:
        return
    try:
        r.setex(
            _draft_key(user_id, conversation_id),
            DRAFT_CACHE_TTL_SECONDS,
            body,
        )
    except Exception:
        logger.exception("Reply draft cache write failed (non-fatal)")


def fetch_cached_draft(user_id: Any, conversation_id: Any) -> Optional[str]:
    r = _redis()
    if r is None:
        return None
    try:
        return r.get(_draft_key(user_id, conversation_id))
    except Exception:
        logger.exception("Reply draft cache read failed (non-fatal)")
        return None


def clear_cached_draft(user_id: Any, conversation_id: Any) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_draft_key(user_id, conversation_id))
    except Exception:
        pass


def _draft_key(user_id: Any, conversation_id: Any) -> str:
    return f"reply_draft:{user_id or 'anon'}:{conversation_id}"


def cache_draft_variant(
    user_id: Any, conversation_id: Any, variant_id: Optional[str]
) -> None:
    """Store the ``email_reply`` prompt-variant id alongside the draft body.

    ``send_reply`` runs in a separate request from ``draft_reply`` and only
    has the (maybe-edited) sent body to work with, so we cache the variant
    id here the same way ``cache_draft_body`` caches the body — send-reply
    reads it back to attribute the outbound Interaction's
    ``insights["prompt_variants"]`` to the variant that actually produced
    the draft.
    """
    if not variant_id:
        return
    r = _redis()
    if r is None:
        return
    try:
        r.setex(
            _draft_variant_key(user_id, conversation_id),
            DRAFT_CACHE_TTL_SECONDS,
            variant_id,
        )
    except Exception:
        logger.exception("Reply draft variant cache write failed (non-fatal)")


def fetch_cached_draft_variant(user_id: Any, conversation_id: Any) -> Optional[str]:
    r = _redis()
    if r is None:
        return None
    try:
        return r.get(_draft_variant_key(user_id, conversation_id))
    except Exception:
        logger.exception("Reply draft variant cache read failed (non-fatal)")
        return None


def clear_cached_draft_variant(user_id: Any, conversation_id: Any) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_draft_variant_key(user_id, conversation_id))
    except Exception:
        pass


def _draft_variant_key(user_id: Any, conversation_id: Any) -> str:
    return f"reply_draft_variant:{user_id or 'anon'}:{conversation_id}"


# ── Diff helpers (used by reply edit-distance) ───────────────────────────


def diff_summary(original: str, updated: str) -> Dict[str, Any]:
    """Return a compact diff representation suitable for the event payload."""
    original = original or ""
    updated = updated or ""
    matcher = difflib.SequenceMatcher(None, original, updated)
    ratio = matcher.ratio()  # 1.0 = identical
    chars_changed = max(len(original), len(updated)) - sum(
        block.size for block in matcher.get_matching_blocks()
    )
    norm = max(len(original), len(updated), 1)
    return {
        "similarity": round(ratio, 4),
        "edit_distance_normalized": round(chars_changed / norm, 4),
        "original_len": len(original),
        "updated_len": len(updated),
    }


def classify_reply_change(original: str, updated: str) -> Tuple[str, Dict[str, Any]]:
    """Map a draft→sent diff to a feedback event_type + payload.

    Buckets (matched to the plan's gold-standard signal):
        - identical       → 'reply_sent_unchanged'
        - small (<= 20%)  → 'reply_edited_before_send' (small)
        - large (> 20%)   → 'reply_edited_before_send' (large)
        - empty/missing   → 'reply_sent_unchanged' (defensive)
    """
    summary = diff_summary(original, updated)
    if summary["original_len"] == 0:
        return "reply_sent_unchanged", summary
    if summary["edit_distance_normalized"] == 0.0:
        return "reply_sent_unchanged", summary
    if summary["edit_distance_normalized"] <= 0.20:
        summary["edit_size"] = "small"
        return "reply_edited_before_send", summary
    summary["edit_size"] = "large"
    return "reply_edited_before_send", summary


# ── Volume over time (live + rollup union) ────────────────────────────────

# Comfortably covers the default raw-retention window
# (``event_retention.FEEDBACK_EVENT_RAW_RETENTION_DAYS`` == 365) plus
# headroom for a per-tenant override, while still bounding the query.
_MAX_VOLUME_DAYS = 3650


async def feedback_volume_by_day(
    db: AsyncSession, tenant_id: Any, days: int = 400
) -> List[Dict[str, Any]]:
    """Daily feedback-event counts for a tenant, per (surface, event_type).

    Unions the live ``feedback_events`` table with ``feedback_daily_rollup``
    — the aggregate the retention sweep writes (see
    ``event_retention.sweep_feedback_events``) right before it deletes raw
    rows older than the retention window. Without this union, a volume
    chart falls off a cliff at the retention horizon even though the daily
    counts are still on disk.

    Live and rollup rows shouldn't normally overlap on the same
    (day, surface, event_type) — the sweep deletes the raw rows it just
    aggregated — but if a day does show up in both (e.g. the sweep is
    mid-run), the rollup count wins as the settled aggregate.
    """
    days = max(1, min(days, _MAX_VOLUME_DAYS))
    cutoff = date.today() - timedelta(days=days)

    live_stmt = (
        select(
            func.date(FeedbackEvent.created_at).label("day"),
            FeedbackEvent.surface,
            FeedbackEvent.event_type,
            func.count().label("count"),
        )
        .where(
            FeedbackEvent.tenant_id == tenant_id,
            FeedbackEvent.created_at >= cutoff,
        )
        .group_by(
            func.date(FeedbackEvent.created_at),
            FeedbackEvent.surface,
            FeedbackEvent.event_type,
        )
    )
    rollup_stmt = select(
        FeedbackDailyRollup.day,
        FeedbackDailyRollup.surface,
        FeedbackDailyRollup.event_type,
        FeedbackDailyRollup.count,
    ).where(
        FeedbackDailyRollup.tenant_id == tenant_id,
        FeedbackDailyRollup.day >= cutoff,
    )

    live_rows = (await db.execute(live_stmt)).all()
    rollup_rows = (await db.execute(rollup_stmt)).all()

    # ``func.date(...)`` comes back as a native ``date`` on Postgres but as
    # plain text on SQLite (used by the test suite) — normalize to an ISO
    # string up front so the two sources merge/sort on a consistent key.
    def _day_str(day: Any) -> str:
        return day.isoformat() if hasattr(day, "isoformat") else str(day)

    merged: Dict[Tuple[str, str, str], int] = {}
    for day, surface, event_type, count in live_rows:
        merged[(_day_str(day), surface, event_type)] = count
    # Rollup wins on overlap — see docstring.
    for day, surface, event_type, count in rollup_rows:
        merged[(_day_str(day), surface, event_type)] = count

    return [
        {"day": day, "surface": surface, "event_type": event_type, "count": count}
        for (day, surface, event_type), count in sorted(
            merged.items(), key=lambda item: item[0]
        )
    ]
