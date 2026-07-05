"""Grace-period re-attach for live media WebSockets (challenge #2c).

Previously any WS disconnect — including a transient network blip —
immediately dispatched batch analysis, which finalises the Interaction
and deletes the Redis transcript buffer. A provider reconnect to the
same session URL then looked like a brand-new call with a reset
timeline.

This module makes disconnect survivable:

* Each WS attach calls :func:`begin_connection`, which increments a
  per-session **connection generation** in Redis and returns the
  accumulated **audio position** (seconds of audio received across all
  prior connections). The handler uses that position to re-anchor the
  paralinguistic window and to rebase Deepgram's word offsets, so the
  diarization timeline stays continuous across reconnects.
* A dirty disconnect records the audio position and schedules a
  **deferred finalizer**: after ``GRACE_PERIOD_SEC`` it re-reads the
  generation and only dispatches batch analysis if no new connection
  arrived in the meantime. A clean provider ``stop`` still finalises
  immediately.
* All state lives in Redis (shared across uvicorn workers), so the
  reconnect may land on a different worker than the finalizer. The
  finalizer itself runs in the process that saw the disconnect; if that
  process dies inside the grace window the session falls back to the
  transcript buffer TTL — acceptable pre-launch, noted in the working
  doc.

Double-dispatch safety: ``_dispatch_batch_analysis`` deletes the
transcript buffer and no-ops when it's absent, so a race between a
deferred finalizer and a provider-side hangup path degrades to a no-op,
not a duplicate Interaction.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Set

logger = logging.getLogger(__name__)

# How long a dropped connection may re-attach before the call is
# finalised. Chosen in docs/complexity/02-realtime-media.md §7.
GRACE_PERIOD_SEC = 45.0

# Resume bookkeeping TTL — comfortably outlives any realistic grace
# window; guards against leaked keys if a finalizer never runs.
_STATE_TTL_SECONDS = 3600


def _gen_key(session_id: str) -> str:
    return f"live:{session_id}:conn_gen"


def _pos_key(session_id: str) -> str:
    return f"live:{session_id}:audio_pos"


@dataclass
class ConnectionAttempt:
    """What :func:`begin_connection` learned about this attach."""

    generation: int
    resume_offset_sec: float
    resumed: bool


async def begin_connection(redis: Any, session_id: str) -> ConnectionAttempt:
    """Register a (re-)attach and return the resume offset.

    Generation 1 with offset 0.0 is a fresh call; anything later means
    a prior connection already streamed audio for this session and the
    caller should rebase its timeline.
    """
    generation = int(await redis.incr(_gen_key(session_id)))
    try:
        await redis.expire(_gen_key(session_id), _STATE_TTL_SECONDS)
    except Exception:
        pass
    raw_pos = await redis.get(_pos_key(session_id))
    try:
        resume_offset = float(raw_pos) if raw_pos is not None else 0.0
    except (TypeError, ValueError):
        resume_offset = 0.0
    return ConnectionAttempt(
        generation=generation,
        resume_offset_sec=resume_offset,
        resumed=generation > 1 and resume_offset > 0.0,
    )


async def record_audio_position(
    redis: Any, session_id: str, seconds: float
) -> None:
    """Persist how much audio this session has received so far — the
    next attach resumes its timeline from here."""
    await redis.set(_pos_key(session_id), repr(float(seconds)), ex=_STATE_TTL_SECONDS)


async def clear_resume_state(redis: Any, session_id: str) -> None:
    """Drop the bookkeeping after a clean stop / final dispatch."""
    await redis.delete(_gen_key(session_id))
    await redis.delete(_pos_key(session_id))


# Strong references so fire-and-forget finalizers aren't GC'd mid-sleep.
_PENDING_FINALIZERS: Set["asyncio.Task"] = set()


def schedule_deferred_finalize(
    *,
    session_id: str,
    generation: int,
    redis_factory: Callable[[], Any],
    dispatch: Callable[[Any, str], Awaitable[None]],
    grace_sec: float = GRACE_PERIOD_SEC,
) -> "asyncio.Task":
    """Finalize ``session_id`` after ``grace_sec`` unless it re-attached.

    ``redis_factory`` builds a fresh connection when the timer fires —
    the caller's connection is closed by then. ``dispatch`` is
    ``_dispatch_batch_analysis`` in production, injected for tests.

    Skips finalization when the stored generation differs from
    ``generation`` (a new connection arrived) **or** the key is gone
    (another path — provider hangup webhook, clean stop — already
    finalised and cleaned up).
    """

    async def _run() -> None:
        await asyncio.sleep(grace_sec)
        redis = redis_factory()
        try:
            current = await redis.get(_gen_key(session_id))
            if current is None:
                logger.debug(
                    "deferred finalize skipped for %s: state already cleared",
                    session_id,
                )
                return
            if int(current) != generation:
                logger.info(
                    "live session %s re-attached within grace window — "
                    "finalize skipped",
                    session_id,
                )
                return
            logger.info(
                "live session %s did not re-attach within %.0fs — finalizing",
                session_id,
                grace_sec,
            )
            await dispatch(redis, session_id)
            await clear_resume_state(redis, session_id)
        except Exception:
            logger.exception("deferred finalize failed for %s", session_id)
        finally:
            try:
                await redis.aclose()
            except Exception:
                pass

    task = asyncio.get_event_loop().create_task(_run())
    _PENDING_FINALIZERS.add(task)
    task.add_done_callback(_PENDING_FINALIZERS.discard)
    return task


__all__ = [
    "GRACE_PERIOD_SEC",
    "ConnectionAttempt",
    "begin_connection",
    "record_audio_position",
    "clear_resume_state",
    "schedule_deferred_finalize",
]
