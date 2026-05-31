"""Celery worker and task definitions — batch processing pipeline.

All async service calls are wrapped with ``asyncio.run()`` because Celery
tasks execute synchronously.  Database access uses a synchronous
SQLAlchemy session created via :func:`_get_sync_session`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_prerun, task_postrun, worker_process_init
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import get_settings
from backend.app.logging_setup import (
    bind_context,
    configure_logging,
    reset_context,
)
from backend.app.observability import init_sentry

configure_logging()
init_sentry()

logger = logging.getLogger(__name__)

settings = get_settings()

# ── Celery app ───────────────────────────────────────────────────────────

celery_app = Celery(
    "linda",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

# TLS Redis (rediss://) requires Celery to be told how to validate
# certs explicitly. redis-py and Celery accept different URL-query
# spellings for this, so we set it via celery config rather than the
# URL. CERT_NONE matches redis-py's default behaviour and is what
# most managed Redis providers (Upstash, ElastiCache) end up using
# in practice. Tighten to CERT_REQUIRED if + when you provision a
# trusted CA bundle in the worker image.
if settings.REDIS_URL.startswith("rediss://"):
    import ssl as _ssl

    celery_app.conf.update(
        broker_use_ssl={"ssl_cert_reqs": _ssl.CERT_NONE},
        redis_backend_use_ssl={"ssl_cert_reqs": _ssl.CERT_NONE},
    )

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # task_track_started was True historically — Celery wrote a STARTED
    # state to the result backend on dequeue, then SUCCESS/FAILURE on
    # completion. We never read STARTED anywhere (grep -r STARTED
    # confirms zero callers in app code; only CRM/audiohook string
    # literals match). Disabling halves task result-backend writes,
    # which is the second-largest contributor to Redis command volume
    # on Upstash's per-command billing.
    task_track_started=False,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Expire task results after 1h. Without this, every Celery result
    # accumulates in Redis forever (default behavior) and bloats memory
    # over weeks. 1h is enough for in-flight chord aggregation and any
    # short-lived consumer (CI, admin tools) that wants to fetch a result.
    result_expires=3600,
    # ── Redis broker tuning for per-command billing ──
    # Celery's default Redis transport polls BRPOP with a ~1s block,
    # which generates one command per worker per second even when the
    # queues are completely idle (4 workers = 345K commands/day on
    # staging baseline, ~70% of Upstash's free-tier 500K cap before
    # any actual work).
    # Raising the BRPOP block timeout to 30s drops that floor by 30x.
    # Trade-off: task pickup latency on an empty queue can be up to
    # 30s; with task_acks_late this only affects how quickly an
    # already-running worker reaches for the *next* task, never how
    # quickly an enqueued task starts running on an idle worker
    # (LPUSH wakes any blocked BRPOP immediately).
    # visibility_timeout is the existing default; we name it
    # explicitly so it doesn't drift if Celery changes upstream.
    broker_transport_options={
        "socket_timeout": 30,
        "socket_connect_timeout": 30,
        "socket_keepalive": True,
        "visibility_timeout": 3600,
        # The poll/BRPOP block window. See comment above.
        "polling_interval": 30.0,
    },
    redis_backend_transport_options={
        "socket_timeout": 30,
        "socket_connect_timeout": 30,
        "socket_keepalive": True,
    },
    # mingle/gossip are worker-to-worker coordination over Redis
    # pub/sub. On a single-worker fly process they're pure overhead
    # (and they spike on every deploy). Disabling saves a startup
    # pub/sub burst per machine restart.
    worker_enable_remote_control=False,
    worker_send_task_events=False,
    beat_schedule={
        # Weekly rollup: every Monday 00:15 UTC, covering the prior Mon–Sun.
        "tenant-insights-weekly": {
            "task": "tenant_insights_weekly",
            "schedule": crontab(minute=15, hour=0, day_of_week=1),
        },
        # ── Scoring / orchestrator cadences ──────────────────────────
        "orchestrator-daily": {
            "task": "orchestrator_daily_all_tenants",
            "schedule": crontab(minute=0, hour=4),
        },
        "orchestrator-weekly": {
            "task": "orchestrator_weekly_all_tenants",
            "schedule": crontab(minute=0, hour=5, day_of_week=1),
        },
        "outcomes-backfill-daily": {
            "task": "outcomes_backfill_all_tenants",
            "schedule": crontab(minute=30, hour=3),
        },
        "calibration-weekly": {
            "task": "calibration_fit_all_tenants",
            "schedule": crontab(minute=0, hour=6, day_of_week=1),
        },
        "irt-calibration-weekly": {
            "task": "irt_fit_all_tenants",
            "schedule": crontab(minute=30, hour=6, day_of_week=1),
        },
        "churn-model-weekly": {
            "task": "churn_train_all_tenants",
            "schedule": crontab(minute=0, hour=7, day_of_week=1),
        },
        # Audio retention runs daily — tenants that care about < 24h audio
        # windows are rare; daily amortizes the S3 list-delete against 23
        # near-noop hourly sweeps.
        "audio-retention-sweep": {
            "task": "audio_retention_sweep",
            "schedule": crontab(minute=15, hour=4),
        },
        # ── Email ingestion ───────────────────────────────────────────
        # Real-time delivery comes from Gmail Pub/Sub + Graph push. This
        # poll is a safety net for integrations whose push subscription
        # hasn't been set up yet — see email_ingest_poll() for the filter.
        "email-ingest-poll": {
            "task": "email_ingest_poll",
            "schedule": 900.0,  # 15 minutes
        },
        # Sample Celery queue depths into Prometheus gauges. Previously
        # every 30s, which generated ~5.7K commands/day for a gauge
        # nobody watches in real time. 5 min is plenty for backpressure
        # alerts (a queue going from 0 to "spiking" is a multi-minute
        # event in practice, not a per-second one) and drops the cost
        # by 10x.
        "sample-queue-depth": {
            "task": "sample_queue_depth",
            "schedule": 300.0,
        },
        # Nightly GDPR-export backup per tenant — lands in the staging
        # bucket under backups/{tenant}/{timestamp}.ndjson.gz. Tenants
        # can opt out via features_enabled.scheduled_backups = False.
        "nightly-tenant-backup": {
            "task": "tenant_backup_all_tenants",
            "schedule": crontab(minute=0, hour=2),
        },
        "email-push-renew": {
            "task": "email_push_renew_subscriptions",
            "schedule": 43200.0,
        },
        # ── Continuous AI improvement ─────────────────────────────────
        # Drain the feedback Redis stream every minute. Previously ran every
        # 30s; the stream is rarely hot and each run costs a task schedule +
        # Redis RTT.
        "consume-feedback-stream": {
            "task": "consume_feedback_stream",
            "schedule": 60.0,
        },
        # Daily sweep of webhook_deliveries + feedback_events. Raw rows
        # age out; feedback_events roll up into feedback_daily_rollup so
        # calibration never loses historical volume.
        "event-retention-daily": {
            "task": "event_retention_sweep",
            "schedule": crontab(minute=45, hour=4),
        },
        "refresh-few-shot-pools": {
            "task": "refresh_few_shot_pools",
            "schedule": crontab(minute=0, hour=3),
        },
        "compute-wer-weekly": {
            "task": "compute_wer_weekly",
            "schedule": crontab(minute=0, hour=2, day_of_week=0),
        },
        "discover-vocabulary-candidates": {
            "task": "discover_vocabulary_candidates",
            "schedule": crontab(minute=0, hour=3, day_of_week=0),
        },
        "vocabulary-digest-weekly": {
            "task": "vocabulary_digest_weekly",
            "schedule": crontab(minute=0, hour=9, day_of_week=1),
        },
        "cross-tenant-aggregate-metrics": {
            "task": "cross_tenant_aggregate_metrics",
            "schedule": crontab(minute=30, hour=0, day_of_week=1),
        },
        "quality-regression-check": {
            "task": "quality_regression_check",
            "schedule": 3600.0,
        },
        "variant-winner-selection": {
            "task": "variant_winner_selection",
            "schedule": crontab(minute=0, hour=4, day_of_week="2,5"),
        },
        "campaign-variant-winner-selection": {
            "task": "campaign_variant_winner_selection",
            "schedule": crontab(minute=15, hour=4, day_of_week="2,5"),
        },
        # ── KB / CRM / telephony cadences ─────────────────────────────
        "vector-health-daily": {
            "task": "vector_health_daily",
            "schedule": crontab(minute=30, hour=0),
        },
        "tenant-brief-refiner-weekly": {
            "task": "tenant_brief_refiner_weekly",
            "schedule": crontab(minute=45, hour=1, day_of_week=1),
        },
        "infer-from-sources-weekly": {
            "task": "infer_from_sources_weekly",
            "schedule": crontab(minute=15, hour=2, day_of_week=1),
        },
        "crm-sync-daily": {
            "task": "crm_sync_daily",
            "schedule": crontab(minute=0, hour=3),
        },
        # Daily trial-expiry sweep at 09:00 UTC (mid-EU day, post-US
        # close). Emits 3/1/0-day notices for sandbox tenants and
        # flips subscription_status="expired" on day 0.
        "trial-expiry-daily": {
            "task": "trial_expiry_daily",
            "schedule": crontab(minute=0, hour=9),
        },
        # ── Manager-view (Phase: manager-view-overhaul) ───────────────
        # Anomaly scan every 15 minutes. Three SQL detectors per tenant
        # against ``interactions.insights``; each detector is a single
        # window query. Cheap. Dial to longer if the false-positive
        # rate climbs after two weeks of real traffic.
        "manager-anomaly-scan": {
            "task": "manager_anomaly_scan_all_tenants",
            "schedule": 900.0,
        },
        # Recommendation builder runs daily right after the orchestrator
        # finishes refreshing the BusinessProfile. One Haiku call per
        # tenant, capped at 5 recommendations.
        "manager-recommendations-build": {
            "task": "manager_recommendations_build",
            "schedule": crontab(minute=30, hour=4),
        },
        # Sweep old recommendations whose 14-day expires_at has passed.
        "manager-recommendations-expire": {
            "task": "manager_recommendations_expire",
            "schedule": crontab(minute=0, hour=3),
        },
        # Auto-resolve manager alerts whose underlying spike has subsided.
        # Frees the partial-unique fingerprint slot so a recurring spike
        # can re-fire.
        "manager-anomaly-resolve": {
            "task": "manager_anomaly_resolve",
            "schedule": crontab(minute=0, hour="*/6"),
        },
    },
)

# ── Worker lifecycle hooks ───────────────────────────────────────────────


@worker_process_init.connect
def _on_worker_start(**_kwargs: Any) -> None:
    """Warm up heavy models so the first task doesn't pay a cold-start tax.

    Two models are worth pre-loading:

    * pyannote.audio speaker-diarization-3.1 (~500 MB) — used by the
      Whisper transcription path for speaker labels.
    * SpeechBrain emotion-recognition-wav2vec2-IEMOCAP (~1 GB) — used
      when a tenant has ``emotion_classification`` enabled.

    We swallow failures so a model fetch outage doesn't refuse the
    worker from starting — individual tasks degrade gracefully when
    the model isn't loaded.

    Set ``LINDA_WORKER_WARMUP=0`` in the env to skip (useful for
    beat-only workers that never run audio tasks).
    """
    if os.environ.get("LINDA_WORKER_WARMUP", "1") == "0":
        logger.info("Worker warmup skipped (LINDA_WORKER_WARMUP=0)")
        return

    # Reconfigure logging + sentry in the fresh process.
    configure_logging()
    init_sentry()

    try:
        from backend.app.services.transcription import _get_diarization_pipeline

        if _get_diarization_pipeline() is not None:
            logger.info("pyannote diarization pipeline preloaded")
    except Exception:
        logger.debug("pyannote warmup failed (non-fatal)", exc_info=True)

    try:
        from backend.app.services.paralinguistics_emotion import (
            prefetch_emotion_classifier,
        )

        if prefetch_emotion_classifier():
            logger.info("speechbrain emotion classifier preloaded")
    except Exception:
        logger.debug("speechbrain warmup failed (non-fatal)", exc_info=True)


@task_prerun.connect
def _on_task_prerun(sender: Any = None, task_id: str = "", args=None, kwargs=None, **_: Any) -> None:
    """Bind correlation ids onto the per-task context.

    We set ``request_id`` to the Celery task id so logs fan-out under
    a single key across the pipeline. Some tasks also take an
    interaction id as the first positional arg — we pick that up
    automatically to keep per-interaction grepping easy.
    """
    values: Dict[str, Optional[str]] = {"request_id": task_id}
    if args:
        first = args[0] if not isinstance(args[0], (int, float, bool)) else None
        if isinstance(first, str) and len(first) == 36:  # likely a UUID
            values["interaction_id"] = first
    # Stash tokens on the task request so postrun can reset them.
    task_request = getattr(sender, "request", None)
    if task_request is not None:
        task_request.linda_context_tokens = bind_context(**values)
    else:
        bind_context(**values)


@task_postrun.connect
def _on_task_postrun(
    sender: Any = None,
    task_id: str = "",
    state: str = "",
    runtime: Optional[float] = None,
    **_: Any,
) -> None:
    task_request = getattr(sender, "request", None)
    tokens = getattr(task_request, "linda_context_tokens", None) if task_request else None
    if tokens:
        reset_context(tokens)

    # Metrics — task name is the Celery-registered name, not the Python
    # function name (matters for aliased tasks).
    try:
        from backend.app.services.metrics import (
            CELERY_TASK_LATENCY,
            CELERY_TASK_RUNS,
        )

        task_name = getattr(sender, "name", "unknown")
        status = "success" if state == "SUCCESS" else (
            "retry" if state == "RETRY" else "failure"
        )
        CELERY_TASK_RUNS.labels(task_name=task_name, status=status).inc()
        if runtime is not None:
            CELERY_TASK_LATENCY.labels(task_name=task_name).observe(float(runtime))
    except Exception:
        logger.debug("task metrics emission failed", exc_info=True)


# ── Synchronous SQLAlchemy session for Celery tasks ──────────────────────

_sync_db_url = settings.DATABASE_URL
# Ensure we use the synchronous driver (psycopg2) rather than asyncpg.
if _sync_db_url.startswith("postgresql+asyncpg://"):
    _sync_db_url = _sync_db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
elif _sync_db_url.startswith("postgres://"):
    pass  # already sync-compatible

# asyncpg accepts ``?ssl=true|require|...``; psycopg2 doesn't — it uses
# ``?sslmode=...``. The async engine in ``db.py`` strips the asyncpg-style
# query params and passes an SSLContext through ``connect_args``; we have
# to do the equivalent for the sync engine. Without this, every Celery
# task fails before its first DB read with "invalid connection option
# 'ssl'", which is silent because tasks just retry forever.
_sync_connect_args: Dict[str, Any] = {
    # TCP keepalive — the worker holds a connection through the whole
    # pipeline, which now includes TWO long idle gaps where no DB
    # traffic flows: (1) the LLM speaker-segmenter (~30-60s Haiku
    # call) and (2) the main analysis (~30-90s Sonnet call). Neon
    # (and any pgbouncer in between) kills idle TCP connections.
    #
    # ``keepalives_idle=10`` — start probing after only 10 seconds
    # of idle, well below Neon's apparent idle cutoff. The earlier
    # 30s threshold left long-text rows still failing intermittently
    # (~1 in 3 attempts) because the connection died before the
    # first probe fired.
    # ``keepalives_interval=5`` — probe every 5s once started.
    # ``keepalives_count=6`` — give up after 6 missed probes (30s).
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 6,
}
if "ssl=" in _sync_db_url or "sslmode=" in _sync_db_url:
    _sync_db_url = _sync_db_url.split("?")[0]
    _sync_connect_args["sslmode"] = "require"

_sync_engine = create_engine(
    _sync_db_url,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    # Recycle pooled connections after 4 minutes. Neon closes long-
    # idle connections server-side; pool_pre_ping catches the corpse
    # on checkout. The TCP keepalive settings above handle the
    # mid-task idle case (long LLM call with no DB activity).
    pool_recycle=240,
    connect_args=_sync_connect_args,
)
_SyncSessionFactory = sessionmaker(bind=_sync_engine, expire_on_commit=False)


def _get_sync_session() -> Session:
    """Return a new synchronous SQLAlchemy session."""
    return _SyncSessionFactory()


# ── Per-task event loop reuse ─────────────────────────────────────────────
#
# ``_run_pipeline`` hits ~8 async entrypoints (triage, analysis, webhook
# emit, scorecards, search-index, …). Each previously called ``asyncio.run``
# directly, which spins up a fresh event loop, re-initializes httpx pools,
# and tears everything down. Running them all inside one loop per task lets
# the anthropic + httpx clients reuse their connection pool for the whole
# run. Typical savings per pipeline: 100–300 ms plus reconnect RTT.
#
# We keep the loop scoped to the task invocation (Celery may run tasks on
# a thread pool; a module-level loop would cross threads).


class _TaskEventLoop:
    """Context manager owning a single event loop for a Celery task.

    Usage::

        with _TaskEventLoop() as loop:
            a = loop.run(_some_coroutine())
            b = loop.run(_another_coroutine())
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def __enter__(self) -> "_TaskEventLoop":
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._loop is None:
            return
        try:
            # Cancel anything still pending so the loop closes cleanly.
            pending = asyncio.all_tasks(self._loop)
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(None)
            self._loop = None

    def run(self, coro):
        assert self._loop is not None, "_TaskEventLoop used outside of `with`"
        return self._loop.run_until_complete(coro)


# Keep Contact.sentiment_trend bounded so the JSONB column doesn't grow
# without limit for long-running customer relationships.
CONTACT_SENTIMENT_TREND_CAP = 50


def update_contact_rollup(contact, insights: Dict[str, Any], created_at) -> None:
    """Append the latest sentiment_score to a contact's trend and bump counts.

    Used both in the live pipeline and in the backfill script so behavior
    stays consistent.  Silently skips non-numeric sentiment_score values.
    """
    sentiment_score = insights.get("sentiment_score") if insights else None
    if sentiment_score is not None:
        try:
            trend = list(contact.sentiment_trend or [])
            trend.append(float(sentiment_score))
            contact.sentiment_trend = trend[-CONTACT_SENTIMENT_TREND_CAP:]
        except (TypeError, ValueError):
            logger.warning(
                "Non-numeric sentiment_score on contact %s: %r",
                getattr(contact, "id", "?"), sentiment_score,
            )
    contact.interaction_count = (contact.interaction_count or 0) + 1
    contact.last_seen_at = created_at


# ── Helper: convert Segment dataclass list → list of dicts ───────────────

def _cleanup_staged_audio(
    session: Session,
    interaction: Any,
    staged_path: Optional[str],
    staged_key: Optional[str],
) -> None:
    """Delete the local tempfile and the S3 staging object.

    Called from the voice task after transcription + paralinguistic
    extraction (or on transcription failure). Idempotent — safe to call
    when either argument is None.
    """
    if staged_path:
        try:
            import os as _os

            _os.unlink(staged_path)
        except Exception:
            logger.debug("tempfile unlink failed: %s", staged_path, exc_info=True)
    if staged_key:
        try:
            from backend.app.services import s3_audio

            s3_audio.delete_object(staged_key)
            if getattr(interaction, "audio_s3_key", None) == staged_key:
                interaction.audio_s3_key = None
                session.commit()
        except Exception:
            logger.warning(
                "S3 staging cleanup failed for %s", staged_key, exc_info=True
            )


def _coerce_seconds(value: Any) -> float:
    """Best-effort convert a snippet/segment time to float seconds.

    The analysis LLM occasionally returns timestamps as ``"MM:SS"`` or
    ``"HH:MM:SS"`` strings instead of float seconds. A naive ``float()``
    raises ``ValueError`` and used to fail step 16 of the pipeline (snippet
    insert), wasting an entire successful Sonnet analysis. Accepts:

    * ``None`` / empty / non-string-non-number → 0.0
    * int / float → coerced to float
    * numeric strings (``"123"``, ``"123.4"``) → float
    * ``"MM:SS"`` / ``"M:SS"`` → minutes * 60 + seconds
    * ``"HH:MM:SS"`` → hours * 3600 + minutes * 60 + seconds

    Anything unparseable returns 0.0 — snippet rows are still useful even
    without precise offsets, and the alternative (raising) takes the
    whole pipeline down.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    if ":" in s:
        try:
            parts = [float(p) for p in s.split(":")]
        except ValueError:
            return 0.0
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _segments_to_dicts(segments: list) -> List[Dict[str, Any]]:
    """Convert transcription Segment objects to plain dicts."""
    result: List[Dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, dict):
            result.append(seg)
        else:
            result.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker_id": seg.speaker_id,
                "confidence": seg.confidence,
            })
    return result


def _compressed_segments_to_text(segments: list) -> str:
    """Join segment texts into a single string for triage."""
    texts: List[str] = []
    for seg in segments:
        if isinstance(seg, dict):
            texts.append(seg.get("text", ""))
        else:
            texts.append(seg.text)
    return " ".join(texts)


def _time_str(seconds: float) -> str:
    """Format seconds as MM:SS string for transcript display."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _segments_for_llm(segments: list) -> List[Dict[str, Any]]:
    """Convert segments to the dict format expected by AI services."""
    result: List[Dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, dict):
            d = dict(seg)
        else:
            d = {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker_id": seg.speaker_id,
                "confidence": seg.confidence,
            }
        # Add formatted time and speaker keys expected by AI services.
        start = d.get("start", 0)
        d.setdefault("time", _time_str(float(start)))
        d.setdefault("speaker", d.get("speaker_id", "Unknown"))
        result.append(d)
    return result


# ── Lazy service instantiation ───────────────────────────────────────────
# Services are created on first use to avoid import-time side effects.

_pii_service = None
_metrics_service = None
_compressor = None
_triage_service = None
_analysis_service = None
_scorecard_service = None
_snippet_service = None
_search_service = None


def _get_pii_service():
    global _pii_service
    if _pii_service is None:
        from backend.app.services.pii_redaction import PIIRedactionService
        _pii_service = PIIRedactionService()
    return _pii_service


def _get_metrics_service():
    global _metrics_service
    if _metrics_service is None:
        from backend.app.services.call_metrics import CallMetricsService
        _metrics_service = CallMetricsService()
    return _metrics_service


def _get_compressor():
    global _compressor
    if _compressor is None:
        from backend.app.services.transcript_compressor import TranscriptCompressor
        _compressor = TranscriptCompressor()
    return _compressor


def _get_triage_service():
    global _triage_service
    if _triage_service is None:
        from backend.app.services.triage_service import TriageService
        _triage_service = TriageService()
    return _triage_service


def _get_analysis_service():
    global _analysis_service
    if _analysis_service is None:
        from backend.app.services.ai_analysis import AIAnalysisService
        _analysis_service = AIAnalysisService()
    return _analysis_service


def _get_scorecard_service():
    global _scorecard_service
    if _scorecard_service is None:
        from backend.app.services.scorecard_service import ScorecardService
        _scorecard_service = ScorecardService()
    return _scorecard_service


def _get_snippet_service():
    global _snippet_service
    if _snippet_service is None:
        from backend.app.services.snippet_service import SnippetService
        _snippet_service = SnippetService()
    return _snippet_service


def _get_search_service():
    global _search_service
    if _search_service is None:
        from backend.app.services.search_service import SearchService
        _search_service = SearchService()
    return _search_service


# ── Core pipeline logic ─────────────────────────────────────────────────

def _run_pipeline(
    session: Session,
    interaction_id: str,
    segments_dicts: List[Dict[str, Any]],
    tenant: Any,
    interaction: Any,
    audio_path: Optional[str] = None,
) -> None:
    """Shared pipeline logic for both voice and text interactions.

    Runs steps 5–16 of the batch pipeline (everything after transcription).

    ``audio_path``, when supplied, is the local file still on disk from
    the transcription step. Used by the paralinguistic extractor (step
    17a). Unused for text interactions.

    All async subcalls (triage, analysis, scorecard, webhook emit, search
    indexing, delta reports, brief rebuild…) run inside a single event
    loop owned by :class:`_TaskEventLoop` so the anthropic + httpx
    clients can reuse their connection pool across steps. See the class
    docstring for why this matters.
    """
    with _TaskEventLoop() as _loop:
        _run_pipeline_impl(
            session,
            interaction_id,
            segments_dicts,
            tenant,
            interaction,
            _loop,
            audio_path=audio_path,
        )


def _run_pipeline_impl(
    session: Session,
    interaction_id: str,
    segments_dicts: List[Dict[str, Any]],
    tenant: Any,
    interaction: Any,
    _loop: "_TaskEventLoop",
    *,
    audio_path: Optional[str] = None,
) -> None:
    from backend.app.models import (
        ActionItem,
        Contact,
        Conversation,
        InteractionScore,
        InteractionSnippet,
        ScorecardTemplate,
    )
    from backend.app.services.transcription import Segment

    tenant_id = str(tenant.id)
    agent_id = str(interaction.agent_id) if interaction.agent_id else ""

    # ── Step 5: PII redaction ────────────────────────────────────────
    # Catch BaseException (not just Exception): heavy ML init paths
    # (spaCy/transformers/presidio) can raise SystemExit on internal
    # failure, which would otherwise terminate the prefork child with
    # exitcode=1 and infinite-loop the task via Celery's acks_late
    # redelivery. PII is a soft step — fall through with the original
    # text and pii_redacted=False if init misbehaves.
    pii_redacted = False
    if tenant.pii_redaction_enabled:
        try:
            pii_config = tenant.pii_redaction_config or {}
            segments_dicts = _get_pii_service().redact_segments(
                segments_dicts, config=pii_config
            )
            pii_redacted = True
            logger.info("PII redaction complete for interaction %s", interaction_id)
        except BaseException:  # noqa: BLE001 — guards against SystemExit
            logger.exception(
                "PII redaction failed for interaction %s — continuing with non-redacted text",
                interaction_id,
            )
            pii_redacted = False

    # ── Step 6: Call metrics ─────────────────────────────────────────
    # Convert dicts back to Segment objects for the metrics service.
    segment_objects: List[Segment] = []
    for sd in segments_dicts:
        segment_objects.append(Segment(
            start=float(sd.get("start", 0)),
            end=float(sd.get("end", 0)),
            text=sd.get("text", ""),
            speaker_id=sd.get("speaker_id"),
            confidence=sd.get("confidence"),
        ))

    call_metrics = _get_metrics_service().compute(segment_objects)
    logger.info("Call metrics computed for interaction %s", interaction_id)

    # ── Step 7: Compress transcript for LLM ──────────────────────────
    compressed_segments = _get_compressor().compress(segment_objects)
    compressed_text = _compressed_segments_to_text(compressed_segments)
    compressed_for_llm = _segments_for_llm(compressed_segments)
    logger.info("Transcript compressed for interaction %s", interaction_id)

    # ── Step 8: Triage — complexity scoring ──────────────────────────
    metadata = {
        "channel": interaction.channel,
        "duration": interaction.duration_seconds,
        "caller_info": interaction.caller_phone or "",
    }
    # Release the connection before this LLM call so Neon doesn't
    # kill it during the 5-15s Haiku round-trip. The next DB query
    # (variant lookup) will check out a fresh connection.
    session.commit()
    triage_result: Dict[str, Any] = _loop.run(
        _get_triage_service().score_complexity(compressed_text, metadata)
    )
    complexity_score = float(triage_result.get("complexity_score", 0.5))
    recommended_tier = triage_result.get("recommended_tier", "sonnet")
    logger.info(
        "Triage complete for interaction %s: score=%.2f tier=%s",
        interaction_id, complexity_score, recommended_tier,
    )

    # ── Step 7.5: Paralinguistic extraction (Phase 2) ────────────────
    # Moved here from the old step 17a so the AI-analysis prompt can
    # consume acoustic features. The flag check happens BEFORE any
    # audio decoding so a tenant who has paralinguistics off pays no
    # cost. Decision matrix Q3/Q4: feature gate + silent fallback.
    paralinguistic_block = None
    paralinguistic_raw: Optional[Dict[str, Any]] = None
    paralinguistic_notable: List[Any] = []
    tenant_features = getattr(tenant, "features_enabled", None) or {}
    if audio_path and tenant_features.get("paralinguistic_analysis", True):
        try:
            from backend.app.services.paralinguistics import (
                SpeakerAudioSegment,
                get_paralinguistic_extractor,
            )
            from backend.app.services.paralinguistic_baseline import (
                analyze as _para_baseline_analyze,
            )
            from backend.app.services.paralinguistic_prompt import (
                build_prompt_block as _build_para_prompt_block,
            )

            para_segments = [
                SpeakerAudioSegment(
                    speaker_id=s.speaker_id or "unknown",
                    start=s.start,
                    end=s.end,
                )
                for s in segment_objects
            ]
            para = get_paralinguistic_extractor().extract(
                para_segments, audio_path=audio_path
            )
            if para.available:
                para_dict = para.as_dict()
                # Deterministic arousal annotation (same as legacy step
                # 17a) — cheap, model-free, applies on every available
                # extraction.
                try:
                    from backend.app.services.paralinguistics_emotion import (
                        annotate_arousal,
                    )
                    para_dict = annotate_arousal(para_dict)
                except Exception:
                    logger.debug("arousal annotation failed", exc_info=True)
                # Heavy SpeechBrain emotion pass stays opt-in.
                if tenant_features.get("emotion_classification"):
                    try:
                        from backend.app.services.paralinguistics_emotion import (
                            annotate_emotion,
                        )
                        speakers = list(
                            (para_dict.get("per_speaker") or {}).keys()
                        )
                        segment_paths = [(sid, audio_path) for sid in speakers]
                        para_dict = annotate_emotion(para_dict, segment_paths)
                    except Exception:
                        logger.debug("emotion annotation failed", exc_info=True)

                paralinguistic_raw = para_dict
                # Per-utterance baselines + outlier detection. Short calls
                # with too few utterances per speaker yield an empty
                # notable list (MIN_SPEAKER_UTTERANCES gate inside).
                _, _, paralinguistic_notable = _para_baseline_analyze(
                    audio_path, segment_objects
                )
                paralinguistic_block = _build_para_prompt_block(
                    para_dict, paralinguistic_notable
                )
                if paralinguistic_block.is_empty():
                    paralinguistic_block = None
        except Exception:
            logger.exception(
                "Paralinguistic extraction failed for %s (non-fatal)",
                interaction_id,
            )
            paralinguistic_block = None

    # ── Step 9: AI analysis ──────────────────────────────────────────
    # Prompt-variant routing + personalization blocks.
    from backend.app.services.ai_analysis import ANALYSIS_SYSTEM_PROMPT_TERSE
    from backend.app.services.personalization_service import (
        build_analysis_context_block,
        build_rag_context_block,
        get_parameter_overrides,
    )
    from backend.app.services.prompt_variant_service import (
        select_variant_sync,
        to_uuid as _variant_to_uuid,
    )

    # Terse clipboard-voice prompt is the default and only path. The
    # verbose ANALYSIS_SYSTEM_PROMPT still lives in ai_analysis.py and
    # can be opted into via TenantPromptConfig.variant_template for
    # tenants that explicitly request the long-form analysis at a
    # higher price tier; it is not exposed via features_enabled.
    variant = select_variant_sync(
        session,
        tenant,
        surface="analysis",
        tier=recommended_tier,
        channel=interaction.channel,
        fallback_template=ANALYSIS_SYSTEM_PROMPT_TERSE,
    )
    tenant_block = build_analysis_context_block(session, tenant)
    rag_block = build_rag_context_block(
        session, tenant, triage_result, channel=interaction.channel
    )
    overrides = get_parameter_overrides(session, tenant, surface="analysis")

    # Tenant + per-customer brief assembled by LINDA agents (complements the
    # prompt-variant tenant_block above — kept as structured dicts so the
    # analyzer can render them in its own cacheable system slots).
    tenant_context = dict(getattr(tenant, "tenant_context", None) or {})
    customer_brief: Dict[str, Any] = {}
    if interaction.contact_id:
        from backend.app.models import Customer as _Customer

        _contact = (
            session.query(Contact)
            .filter(Contact.id == interaction.contact_id)
            .first()
        )
        if _contact and _contact.customer_id:
            _customer = session.query(_Customer).filter(_Customer.id == _contact.customer_id).first()
            if _customer:
                customer_brief = dict(_customer.customer_brief or {})

    # Release the connection before the main analysis — this is the
    # longest LLM call in the pipeline (30-90s on Sonnet). Without
    # this, the connection sits idle during analyze() and Neon kills
    # it before the post-analysis save can run.
    session.commit()
    # Resolve the call date so the prompt can anchor 'Thursday' /
    # 'tomorrow' references to real YYYY-MM-DD due_dates. We prefer
    # ``interaction.started_at`` (set by telephony / upload paths) and
    # fall back to ``created_at`` which always exists.
    _call_dt = getattr(interaction, "started_at", None) or interaction.created_at
    _call_date_str = _call_dt.date().isoformat() if _call_dt else None

    insights: Dict[str, Any] = _loop.run(
        _get_analysis_service().analyze(
            compressed_for_llm,
            tier=overrides.get("force_tier") or recommended_tier,
            triage_result=triage_result,
            system_prompt_override=variant.prompt_template,
            tenant_context_block=tenant_block,
            rag_context_block=rag_block,
            max_tokens_override=overrides.get("max_tokens"),
            tenant_context=tenant_context,
            customer_brief=customer_brief,
            paralinguistic_block=paralinguistic_block,
            complexity_score=complexity_score,
            call_date=_call_date_str,
        )
    )
    interaction.prompt_variant_id = _variant_to_uuid(variant.variant_id)
    logger.info(
        "AI analysis complete for interaction %s (variant=%s status=%s)",
        interaction_id, variant.name, variant.status,
    )

    # ── Step 9b: Outcome inference ──────────────────────────────────
    # Squeeze the analysis JSON into a normalised outcome label and,
    # where warranted, emit CustomerOutcomeEvent rows that downstream
    # agents (TenantBriefRefiner, CustomerBriefBuilder) will read.
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from backend.app.models import CustomerOutcomeEvent
    from backend.app.services.kb.outcome_inference import infer_outcome

    inferred = infer_outcome(insights)
    interaction.outcome_type = inferred.outcome_type
    interaction.outcome_value = inferred.outcome_value
    interaction.outcome_confidence = inferred.outcome_confidence
    interaction.outcome_source = "ai_inferred"
    interaction.outcome_notes = inferred.outcome_notes
    interaction.outcome_captured_at = _dt.now(_tz.utc)

    cust_id_for_rebuild: Optional[uuid.UUID] = None
    if interaction.contact_id:
        contact_row = (
            session.query(Contact)
            .filter(Contact.id == interaction.contact_id)
            .first()
        )
        cust_id_for_rebuild = (
            contact_row.customer_id if contact_row is not None else None
        )
        if cust_id_for_rebuild:
            from backend.app.services.webhook_events import (
                CUSTOMER_OUTCOME_EVENT_MAP,
            )

            customer_events_for_webhooks: List[Dict[str, Any]] = []
            for ev in inferred.customer_events:
                session.add(
                    CustomerOutcomeEvent(
                        tenant_id=tenant.id,
                        customer_id=cust_id_for_rebuild,
                        interaction_id=interaction.id,
                        event_type=ev["event_type"],
                        magnitude=ev.get("magnitude"),
                        signal_strength=ev.get("signal_strength"),
                        reason=ev.get("reason"),
                        source=ev.get("source", "ai_inferred"),
                    )
                )
                wh_event = CUSTOMER_OUTCOME_EVENT_MAP.get(ev["event_type"])
                if wh_event:
                    customer_events_for_webhooks.append(
                        {
                            "webhook_event": wh_event,
                            "customer_id": str(cust_id_for_rebuild),
                            "interaction_id": str(interaction.id),
                            "event_type": ev["event_type"],
                            "reason": ev.get("reason"),
                            "signal_strength": ev.get("signal_strength"),
                            "source": ev.get("source", "ai_inferred"),
                        }
                    )

            # Fan out lifecycle events to subscribed webhooks.
            if customer_events_for_webhooks:
                from backend.app.db import async_session
                from backend.app.services.webhook_dispatcher import emit_event

                async def _emit_lifecycle() -> None:
                    async with async_session() as db:
                        for ev in customer_events_for_webhooks:
                            await emit_event(
                                db,
                                tenant.id,
                                ev["webhook_event"],
                                {k: v for k, v in ev.items() if k != "webhook_event"},
                            )

                try:
                    _loop.run(_emit_lifecycle())
                except Exception:
                    logger.exception("Customer lifecycle webhook emission failed")

    # Kick a debounced customer-brief rebuild so LINDA has a fresh dossier
    # for the next call with this customer. Best-effort; if Redis/Celery are
    # unavailable in this env we'll catch up on the next interaction.
    if cust_id_for_rebuild is not None:
        try:
            from backend.app.services.kb.context_dispatch import (
                schedule_customer_brief_rebuild,
            )

            _loop.run(schedule_customer_brief_rebuild(tenant.id, cust_id_for_rebuild))
        except Exception:
            logger.debug("schedule_customer_brief_rebuild failed", exc_info=True)

    # ── Step 10: Scorecard scoring ───────────────────────────────────
    # All active templates are scored in a single batched Haiku call — one
    # call per interaction instead of one per template. Transcript + insights
    # are shipped once; the model returns per-template results. If the
    # batched response parses poorly, score_many falls back to per-template
    # calls transparently so a flaky template doesn't take out siblings.
    templates = (
        session.query(ScorecardTemplate)
        .filter(ScorecardTemplate.tenant_id == tenant.id)
        .all()
    )
    applicable_templates: List[Dict[str, Any]] = []
    for template in templates:
        channel_filter = template.channel_filter
        if channel_filter and interaction.channel not in channel_filter:
            continue
        applicable_templates.append(
            {
                "id": str(template.id),
                "name": template.name,
                "criteria": template.criteria,
            }
        )

    transcript_for_scoring = _segments_for_llm(segments_dicts)
    if applicable_templates:
        scorecard_results = _loop.run(
            _get_scorecard_service().score_many(
                transcript_for_scoring, applicable_templates, insights
            )
        )
    else:
        scorecard_results = []
    logger.info(
        "Scored %d scorecard templates for interaction %s",
        len(scorecard_results), interaction_id,
    )

    # ── Step 11: Snippet identification ──────────────────────────────
    snippet_dicts = _get_snippet_service().identify_notable_segments(
        insights, agent_id, tenant_id
    )
    logger.info(
        "Identified %d snippets for interaction %s",
        len(snippet_dicts), interaction_id,
    )

    # ── Step 12: Search indexing ─────────────────────────────────────
    search_data = {
        "transcript_segments": segments_dicts,
        "summary": insights.get("summary", ""),
        "topics": [
            t.get("name", t) if isinstance(t, dict) else t
            for t in insights.get("topics", [])
        ],
        "agent_id": agent_id,
        "channel": interaction.channel,
        "sentiment_score": insights.get("sentiment_score"),
        "created_at": (
            interaction.created_at.isoformat()
            if interaction.created_at else None
        ),
    }
    try:
        _loop.run(
            _get_search_service().index_interaction(
                interaction_id, tenant_id, search_data
            )
        )
        logger.info("Search indexed interaction %s", interaction_id)
    except Exception:
        logger.exception(
            "Search indexing failed for interaction %s (non-fatal)",
            interaction_id,
        )

    # ── Step 12b: Entity resolution ──────────────────────────────────
    # Plug Customer + Contact + role inference into the row before we
    # flip status to "analyzed". The resolver is best-effort: any
    # exception becomes a logged warning, the interaction still lands
    # as analyzed, and the orphan can be reprocessed later. Runs the
    # contact-rollup step (13b) against the freshly-resolved contact.
    try:
        from backend.app.services.entity_resolution import resolve_interaction_entities

        resolution = _loop.run(
            resolve_interaction_entities(
                session=session,
                interaction=interaction,
                tenant=tenant,
                insights=insights,
                compressed_transcript=compressed_text,
            )
        )
        if resolution.customer_action != "none":
            logger.info(
                "Entity resolution for interaction %s: %s (score=%.2f, customer_id=%s)",
                interaction_id,
                resolution.customer_action,
                resolution.customer_score,
                resolution.customer_id,
            )
        if resolution.suggestions:
            # Stash suggestions on the interaction so the SPA can
            # render the inline match-candidate card. The
            # notification-tray surface plugs into the same data via
            # a dedicated endpoint in the Suggestions phase.
            existing_meta = (
                getattr(interaction, "insights", None) or {}
            )
            existing_meta = dict(existing_meta)
            existing_meta["entity_resolution_suggestions"] = resolution.suggestions
            interaction.insights = existing_meta
    except Exception:
        logger.exception(
            "Entity resolution failed for interaction %s — continuing as orphan",
            interaction_id,
        )

    # ── Step 12c: Warnings + commitments ─────────────────────────────
    # Run after entity_resolution so we have ``interaction.customer_id``
    # populated (the warnings engine needs it to attach rows). Same
    # best-effort posture as 12b: any flake becomes a logged warning,
    # the interaction still lands as analyzed.
    try:
        from backend.app.services.warnings_commitments import detect_and_persist

        wc_outcome = _loop.run(
            detect_and_persist(
                session=session,
                interaction=interaction,
                tenant=tenant,
                insights=insights,
                compressed_transcript=compressed_text,
            )
        )
        if (
            wc_outcome.warnings_upserted
            or wc_outcome.commitments_created
            or wc_outcome.commitments_marked_done
        ):
            logger.info(
                "Phase 4 detect for interaction %s: warnings=%d (re-raised %d) "
                "commitments=%d done=%d",
                interaction_id,
                wc_outcome.warnings_upserted,
                wc_outcome.warnings_re_raised,
                wc_outcome.commitments_created,
                wc_outcome.commitments_marked_done,
            )
    except Exception:
        logger.exception(
            "Warnings/commitments detection failed for interaction %s — continuing",
            interaction_id,
        )

    # ── Step 13: Update interaction row ──────────────────────────────
    interaction.status = "analyzed"
    interaction.transcript = segments_dicts
    # Phase 1: the LLM emits coarse buckets only. Code maps them to the
    # numeric fields the rest of the platform consumes (analytics,
    # contact health rollups, dashboards, training labels). Done BEFORE
    # the dict copy so downstream reads off the same ``insights`` ref
    # below see the derived numerics.
    from backend.app.services.score_mapping import derive_numeric_scores
    derive_numeric_scores(insights)
    # Phase 3 foundation: derive deterministic rubric scores from the
    # LLM's evidence counts (objections / commitments / discovery
    # questions, etc.) and dual-log them alongside the bucket-mapped
    # values. Lets us validate calibration before flipping to
    # rubric-as-source-of-truth in Phase 4.
    from backend.app.services.evidence_scoring import attach_rubric
    attach_rubric(insights)
    # Phase 5 rapport gauge — Linguistic Style Matching (LSM) is a
    # transcript-only signal of how closely the rep and customer mirror
    # each other's function-word usage. Phase 2 adds the
    # vocal-accommodation half from per-speaker prosody when audio
    # was available. The composite ``rapport.overall`` blends both
    # halves automatically.
    from backend.app.services.rapport_lsm import (
        attach_rapport,
        attach_vocal_accommodation,
    )
    attach_rapport(insights, segments_dicts)
    attach_vocal_accommodation(insights, paralinguistic_raw)

    # Phase 4 binary classifiers. When an active model exists for the
    # tenant + target, we write a calibrated probability alongside the
    # bucket-mapped numeric (dual-logging). Cold-start tenants get
    # ``status="insufficient_data"`` and the rubric / bucket numerics
    # remain the source of truth — no override. Best-effort: a missing
    # ScorerVersion table or a malformed model row never blocks the
    # pipeline.
    try:
        from backend.app.services.phase4_classifier import predict as _phase4_predict

        classifier_block: Dict[str, Dict[str, Any]] = {}
        for _target in ("churn", "upsell"):
            pred = _phase4_predict(session, tenant.id, _target, insights)
            classifier_block[_target] = {
                "status": pred.status,
                "probability": pred.probability,
                "model_version": pred.model_version,
                "label_horizon_days": pred.label_horizon_days,
                "caveat": pred.caveat,
            }
        insights["classifier_predictions"] = classifier_block
    except Exception:
        logger.exception(
            "Phase 4 classifier inference failed for %s (non-fatal)",
            interaction_id,
        )

    # ``interaction.insights`` may already have been mutated above
    # (entity_resolution stashes suggestions there); merge rather than
    # overwrite when we replace the dict here.
    merged_insights = dict(insights)
    existing_extras = getattr(interaction, "insights", None) or {}
    if isinstance(existing_extras, dict):
        for k in (
            "entity_resolution_suggestions",
            "entity_resolution_debug",
            "warnings_commitments_debug",
        ):
            if k in existing_extras:
                merged_insights[k] = existing_extras[k]
    interaction.insights = merged_insights
    interaction.call_metrics = call_metrics
    interaction.complexity_score = complexity_score
    interaction.analysis_tier = recommended_tier
    interaction.pii_redacted = pii_redacted

    # ── Step 13b: Update contact trend rollup ────────────────────────
    if interaction.contact_id is not None:
        contact = session.query(Contact).filter(Contact.id == interaction.contact_id).first()
        if contact is not None:
            update_contact_rollup(contact, insights, interaction.created_at)

    # ── Step 13c: Update conversation rollup (email threading) ───────
    if interaction.conversation_id is not None:
        conv = (
            session.query(Conversation)
            .filter(Conversation.id == interaction.conversation_id)
            .first()
        )
        if conv is not None:
            # Keep a small rolling summary + sentiment series at the conv level
            # so the reply generator and UI don't have to aggregate on read.
            conv_insights = dict(conv.insights or {})
            series = list(conv_insights.get("sentiment_series") or [])
            sscore = insights.get("sentiment_score")
            if sscore is not None:
                try:
                    series.append(float(sscore))
                    conv_insights["sentiment_series"] = series[-50:]
                except (TypeError, ValueError):
                    pass
            conv_insights["latest_summary"] = insights.get("summary", "")
            conv_insights["latest_churn_risk"] = insights.get("churn_risk")
            conv_insights["latest_upsell_score"] = insights.get("upsell_score")
            conv.insights = conv_insights
            # Direction drives status: inbound customer → waiting on us;
            # outbound agent → waiting on customer.
            if interaction.direction == "inbound":
                conv.status = "waiting_agent"
            elif interaction.direction == "outbound":
                conv.status = "waiting_customer"

    # ── Step 14: Insert action items ─────────────────────────────────
    # Capture every field the LLM emits; prior code dropped ``due_date``,
    # ``email_draft``, and the new advanced fields on the floor.
    for ai_item in insights.get("action_items", []):
        # Parse due_date if provided as a string. Tolerate malformed
        # values by leaving the column NULL rather than failing.
        raw_due = ai_item.get("due_date")
        parsed_due = None
        if isinstance(raw_due, str) and raw_due.strip():
            try:
                parsed_due = date.fromisoformat(raw_due.strip())
            except ValueError:
                parsed_due = None

        # email_draft can arrive as the new {subject, body} dict or as
        # the legacy plain string under either ``email_draft`` or
        # ``suggested_email_draft``. Normalize to the dict shape stored
        # in JSONB.
        raw_email = ai_item.get("email_draft") or ai_item.get("suggested_email_draft")
        if isinstance(raw_email, str):
            email_draft = {"subject": "", "body": raw_email}
        elif isinstance(raw_email, dict):
            email_draft = raw_email
        else:
            email_draft = None

        raw_category = ai_item.get("category")
        # Normalize category through the taxonomy service: known aliases
        # map to canonical names; unknown strings get logged as candidates
        # for promotion. Failures are non-fatal — we keep the raw value.
        canonical_category = raw_category
        try:
            from backend.app.services.category_taxonomy import record_occurrence
            normalized = record_occurrence(session, tenant.id, raw_category or "")
            if normalized:
                canonical_category = normalized
        except Exception:
            logger.debug(
                "category taxonomy lookup failed for %r (non-fatal)",
                raw_category, exc_info=True,
            )

        action = ActionItem(
            interaction_id=interaction.id,
            tenant_id=tenant.id,
            title=ai_item.get("title", "Untitled"),
            description=ai_item.get("description", ""),
            category=canonical_category,
            priority=ai_item.get("priority", "medium"),
            status="open",
            due_date=parsed_due,
            email_draft=email_draft,
            call_script=ai_item.get("call_script") or None,
            next_step_type=ai_item.get("next_step_type"),
            recommended_channel=ai_item.get("recommended_channel"),
            channel_reasoning=ai_item.get("channel_reasoning"),
            participants=ai_item.get("participants") or [],
            prep_artifacts=ai_item.get("prep_artifacts") or [],
            implicit_signal=ai_item.get("implicit_signal"),
            suggested_attachments=ai_item.get("suggested_attachments") or [],
            manually_created=False,
        )
        session.add(action)

    # ── Step 14a: Synthesize Action Plan (new DAG-based workflow) ────
    # The Action Plan is the DAG-based successor to ActionItem. It runs
    # alongside ActionItem during the cutover so consumers can migrate
    # at their own pace; both surfaces stay populated for the same
    # interaction. Per the locked failure-mode decision, plan synthesis
    # never blocks the pipeline — on any error we log and continue;
    # the user just sees the legacy action_items list until the next
    # call goes through cleanly.
    #
    # The synthesizer wants an AsyncSession; the pipeline holds a sync
    # one. We open a short-lived async session over the same database
    # and commit its changes independently — they're orthogonal to the
    # sync session's writes (different rows).
    # Diagnostic stamp on interaction.insights so we can read the
    # synthesis trace via the standard /interactions/{id} endpoint
    # without needing Fly log access. Each phase writes a key; the
    # final state of these keys after a redrive tells us exactly
    # where synthesis dropped (or whether it never started).
    _plan_diag: Dict[str, Any] = {"entered_block": True}

    # synth-redeploy-marker-2026-05-31
    try:
        from backend.app.db import async_session as _async_session_factory
        from backend.app.services.action_plan.synthesizer import (
            ActionPlanSynthesizer,
            SynthesisFailedError,
            SynthesisInputs,
        )

        # COMMIT (not flush) sync-session writes before opening a
        # separate async connection. The async session checks out a
        # different asyncpg connection from the pool, and under
        # PostgreSQL's default READ COMMITTED isolation it cannot see
        # uncommitted writes from the sync connection's transaction.
        # If we only flush, ``async_db.get(Interaction, id)`` below
        # returns None (the row was created/updated in the still-open
        # sync transaction) and synthesis silently exits via the
        # early-return guard, leaving no ActionPlan and no log line.
        session.commit()
        _plan_diag["sync_commit_ok"] = True

        async def _run_plan_synthesis() -> None:
            async with _async_session_factory() as async_db:
                synthesizer = ActionPlanSynthesizer()
                # Re-fetch the tenant + interaction from the async
                # session so the ORM identity map is clean.
                from backend.app.models import Interaction as _IxModel
                from backend.app.models import Tenant as _TenantModel

                async_tenant = await async_db.get(_TenantModel, tenant.id)
                async_ix = await async_db.get(_IxModel, interaction.id)
                _plan_diag["async_session_opened"] = True
                _plan_diag["async_tenant_loaded"] = async_tenant is not None
                _plan_diag["async_ix_loaded"] = async_ix is not None
                if async_tenant is None or async_ix is None:
                    # Loud log so this can't go unnoticed again. If we
                    # ever see "tenant_missing=True" or "ix_missing=True"
                    # in Fly logs, the commit-vs-flush guard above
                    # regressed.
                    logger.warning(
                        "Action plan synthesis early-return for interaction %s: "
                        "tenant_missing=%s ix_missing=%s (sync session must commit "
                        "before opening async session)",
                        interaction.id,
                        async_tenant is None,
                        async_ix is None,
                    )
                    _plan_diag["early_return_reason"] = "rows_not_visible"
                    return
                # compressed_for_llm is a LIST of segment dicts produced
                # by _segments_for_llm(), not a string. Flatten to plain
                # text so the synthesizer's retrieval query (and Call A's
                # transcript_text input) gets the right shape. Fall back
                # to interaction.raw_text when compressed_for_llm isn't
                # bound (paths that bypass segmentation).
                try:
                    _segs = compressed_for_llm
                except NameError:
                    _segs = None
                if isinstance(_segs, list):
                    _lines = []
                    for _s in _segs:
                        if isinstance(_s, dict):
                            t = _s.get("text") or ""
                            spk = _s.get("speaker") or _s.get("speaker_id")
                            if spk:
                                _lines.append(f"{spk}: {t}")
                            else:
                                _lines.append(str(t))
                    txt = "\n".join(_lines)
                elif isinstance(_segs, str):
                    txt = _segs
                else:
                    txt = (async_ix.raw_text or "")
                logger.info(
                    "Action plan synthesis entering synthesize() for interaction %s",
                    interaction.id,
                )
                _plan_diag["synthesize_started"] = True
                _result = await synthesizer.synthesize(
                    async_db,
                    SynthesisInputs(
                        tenant=async_tenant,
                        interaction=async_ix,
                        transcript_text=txt,
                        triage=(triage_result if isinstance(triage_result, dict) else {}),
                        customer_id=async_ix.customer_id,
                        acting_user_id=async_ix.agent_id,
                    ),
                )
                _plan_diag["synthesize_returned"] = True
                _plan_diag["new_plan_id"] = str(_result.plan_id)
                _plan_diag["new_step_count"] = len(_result.steps or [])
                await async_db.commit()
                _plan_diag["async_commit_ok"] = True
                logger.info(
                    "Action plan synthesis SUCCESS for interaction %s: plan_id=%s steps=%d domain=%s",
                    interaction.id,
                    _result.plan_id,
                    len(_result.steps or []),
                    _result.chosen_domain,
                )

        _loop.run(_run_plan_synthesis())
    except SynthesisFailedError as _plan_exc:  # type: ignore[name-defined]
        logger.warning(
            "Action plan synthesis failed for interaction %s (non-fatal): %s",
            interaction.id, _plan_exc,
        )
        _plan_diag["caught_kind"] = "SynthesisFailedError"
        _plan_diag["caught_error"] = str(_plan_exc)[:200]
    except Exception as _plan_exc_other:  # noqa: BLE001
        logger.exception(
            "Action plan synthesis raised unexpectedly for interaction %s "
            "(non-fatal); pipeline continues",
            interaction.id,
        )
        _plan_diag["caught_kind"] = type(_plan_exc_other).__name__
        _plan_diag["caught_error"] = str(_plan_exc_other)[:200]
        import traceback as _tb
        _plan_diag["caught_traceback"] = _tb.format_exc()[:2000]
    finally:
        # Persist the diagnostic so we can read it via the standard
        # interaction-detail endpoint. This is the only signal we
        # currently have visibility into without Fly log access.
        #
        # We've observed the previous read-mutate-write approach on
        # the main session silently failing to land the diag even
        # when synthesis succeeded. Suspected cause: the main session
        # had pending state (or a later commit in this task wiped
        # the transaction containing the diag write). Fix is two-
        # fold: (1) open a brand-new sync session isolated from
        # whatever state the main session is in, (2) use a server-
        # side JSONB merge (``insights || jsonb_build_object(...)``)
        # so we don't need to read the current insights value at
        # all -- the merge happens atomically in Postgres and
        # preserves any concurrent writes to other top-level keys.
        try:
            import json as _json
            from sqlalchemy import text as _sql_text
            _diag_session = _SyncSessionFactory()
            try:
                _diag_session.execute(
                    _sql_text(
                        "UPDATE interactions SET insights = "
                        "COALESCE(insights, '{}'::jsonb) "
                        "|| jsonb_build_object('_plan_synthesis_diag', "
                        "CAST(:diag AS jsonb)) "
                        "WHERE id = :iid"
                    ),
                    {"diag": _json.dumps(_plan_diag), "iid": interaction.id},
                )
                _diag_session.commit()
            finally:
                _diag_session.close()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to stamp _plan_synthesis_diag on interaction (fresh-session path)"
            )

    # ── Step 15: Insert interaction scores ───────────────────────────
    for sc in scorecard_results:
        score_row = InteractionScore(
            interaction_id=interaction.id,
            template_id=uuid.UUID(sc["template_id"]),
            tenant_id=tenant.id,
            total_score=sc.get("total_score"),
            criterion_scores=sc.get("criterion_scores", []),
        )
        session.add(score_row)

    # ── Step 16: Insert interaction snippets ─────────────────────────
    for sn in snippet_dicts:
        snippet_row = InteractionSnippet(
            interaction_id=interaction.id,
            tenant_id=tenant.id,
            start_time=_coerce_seconds(sn.get("start_time")),
            end_time=_coerce_seconds(sn.get("end_time")),
            snippet_type=sn.get("snippet_type"),
            quality=sn.get("quality"),
            title=sn.get("title"),
            description=sn.get("description"),
            transcript_excerpt=sn.get("transcript_excerpt", []),
            tags=sn.get("tags", []),
            in_library=sn.get("in_library", False),
            library_category=sn.get("library_category"),
        )
        session.add(snippet_row)

    # ── Step 17: Write InteractionFeatures (canonical feature store) ─
    from backend.app.models import InteractionFeatures
    from backend.app.services.feature_extractors import FeatureExtractor

    deterministic_features = FeatureExtractor().extract(segment_objects)

    # ── Step 17a (legacy): persist paralinguistic block ──────────────
    # Phase 2 moved the extraction itself to step 7.5 so the AI prompt
    # could consume it. This step now only persists the already-
    # computed ``paralinguistic_raw`` (plus the notable utterance list)
    # onto ``deterministic_features`` so the existing JSONB storage and
    # downstream consumers (rapport gauge, dashboards) keep working.
    if paralinguistic_raw:
        block_for_storage = dict(paralinguistic_raw)
        if paralinguistic_notable:
            block_for_storage["notable_utterances"] = [
                {
                    "segment_idx": tag.segment_idx,
                    "speaker_id": tag.speaker_id,
                    "start": tag.start,
                    "features": [
                        {"name": name, "z": z} for name, z in tag.features
                    ],
                }
                for tag in paralinguistic_notable
            ]
        deterministic_features["paralinguistic"] = block_for_storage

    features_row = (
        session.query(InteractionFeatures)
        .filter(InteractionFeatures.interaction_id == interaction.id)
        .first()
    )
    if features_row is None:
        features_row = InteractionFeatures(
            interaction_id=interaction.id,
            tenant_id=tenant.id,
        )
        session.add(features_row)

    # ── Step 17c: Fire outbound webhooks ───────────────────────────────
    _emit_webhooks_for_interaction(
        tenant_id=tenant.id,
        interaction_id=uuid.UUID(interaction_id),
        insights=insights,
        outcome_type=interaction.outcome_type,
        outcome_confidence=interaction.outcome_confidence,
    )

    # ── Step 17d: CRM write-back (opt-in) ──────────────────────────────
    # Dispatch as a separate Celery task so a slow CRM doesn't stretch
    # the critical path. Idempotent against the same interaction, so a
    # Celery retry can replay safely.
    tf = getattr(tenant, "features_enabled", None) or {}
    if tf.get("crm_writeback_notes") or tf.get("crm_writeback_activities"):
        try:
            crm_writeback.delay(interaction_id)
        except Exception:
            logger.debug(
                "CRM write-back dispatch failed for %s", interaction_id,
                exc_info=True,
            )

    # ── Step 17b: Weak-supervision labels (orthogonal to the LLM guess) ─
    # Cheap regex LFs produce {cancel_intent, commitment, objection_resolved}
    # labels stored alongside the LLM structured blob.  The orchestrator
    # and calibrator treat these as an independent signal, improving
    # calibration quality without replacing any existing field.
    from backend.app.services.weak_supervision import label_interaction

    enriched_insights: Dict[str, Any] = dict(insights or {})
    try:
        ws_labels = label_interaction(
            transcript=compressed_text,
            turns=segments_dicts,
            llm_churn_signal=enriched_insights.get("churn_risk_signal"),
        )
        enriched_insights["weak_supervision"] = {
            key: {
                "label": agg.label,
                "probability": agg.probability,
                "support": agg.support,
                "lf_votes": agg.lf_votes,
            }
            for key, agg in ws_labels.items()
        }
    except Exception:  # noqa: BLE001 — WS must never fail the pipeline
        logger.exception(
            "Weak-supervision labeling failed for interaction %s (non-fatal)",
            interaction_id,
        )

    features_row.deterministic = deterministic_features
    features_row.llm_structured = enriched_insights
    features_row.scorer_versions = {
        "analysis_tier": recommended_tier,
        "complexity_score": complexity_score,
    }
    # Phase 0 telemetry: record which prompt + model produced this analysis,
    # so outcome data can be cohorted by version when training the Phase 4
    # classifier. Imported lazily to avoid pulling LLM-client deps in the
    # test environment.
    try:
        from backend.app.services.ai_analysis import (
            ANALYSIS_PROMPT_VERSION,
            MODELS as _ANALYSIS_MODELS,
        )
        from backend.app.services.triage_service import TRIAGE_PROMPT_VERSION

        features_row.analysis_prompt_version = ANALYSIS_PROMPT_VERSION
        features_row.triage_prompt_version = TRIAGE_PROMPT_VERSION
        features_row.model_used = _ANALYSIS_MODELS.get(recommended_tier)
    except Exception:
        logger.debug(
            "telemetry version capture failed for %s (non-fatal)",
            interaction_id,
            exc_info=True,
        )

    # ── Step 18: Enqueue delta report → orchestrator ─────────────────
    try:
        _enqueue_delta_report(
            session=session,
            tenant=tenant,
            interaction=interaction,
            features={
                "deterministic": deterministic_features,
                "llm_structured": enriched_insights,
            },
            _loop=_loop,
        )
    except Exception:
        logger.exception(
            "Delta report generation failed for %s (non-fatal)",
            interaction_id,
        )

    # ── Step 19: Fire outbound webhooks ──────────────────────────────
    from backend.app.services.webhook_dispatcher import dispatch_sync

    analyzed_payload = {
        "event": "interaction.analyzed",
        "tenant_id": tenant_id,
        "interaction_id": interaction_id,
        "channel": interaction.channel,
        "direction": interaction.direction,
        "classification": getattr(interaction, "classification", None),
        "contact_id": str(interaction.contact_id) if interaction.contact_id else None,
        "conversation_id": (
            str(interaction.conversation_id) if interaction.conversation_id else None
        ),
        "summary": insights.get("summary"),
        "sentiment_score": insights.get("sentiment_score"),
        "churn_risk": insights.get("churn_risk"),
        "upsell_score": insights.get("upsell_score"),
        "action_item_count": len(insights.get("action_items", [])),
    }
    try:
        dispatch_sync(session, tenant.id, "interaction.analyzed", analyzed_payload)
    except Exception:
        logger.exception("Webhook dispatch raised (non-fatal)")

    # Conversation-level fan-out — only when this message actually has a thread.
    if interaction.conversation_id is not None:
        conv_row = (
            session.query(Conversation)
            .filter(Conversation.id == interaction.conversation_id)
            .first()
        )
        if conv_row is not None:
            try:
                dispatch_sync(
                    session,
                    tenant.id,
                    "conversation.updated",
                    {
                        "event": "conversation.updated",
                        "tenant_id": tenant_id,
                        "conversation_id": str(conv_row.id),
                        "channel": conv_row.channel,
                        "classification": conv_row.classification,
                        "status": conv_row.status,
                        "message_count": conv_row.message_count,
                        "latest_summary": (conv_row.insights or {}).get("latest_summary"),
                    },
                )
            except Exception:
                logger.exception("Conversation webhook dispatch raised (non-fatal)")

    session.commit()
    logger.info("Pipeline complete for interaction %s", interaction_id)

    # ── Step 18: Schedule LLM-judge evaluation (Layer 2) ─────────────
    # 15-min delay so the interaction settles in DB and (for replies) any
    # follow-on edit-distance event has been written.
    try:
        evaluate_analysis.apply_async(args=[interaction_id], countdown=900)
        if interaction.channel == "email":
            evaluate_classification.apply_async(args=[interaction_id], countdown=900)
            if interaction.direction == "outbound":
                evaluate_reply.apply_async(args=[interaction_id], countdown=900)
    except Exception:
        logger.exception("Failed to enqueue evaluator tasks (non-fatal)")


def _enqueue_delta_report(
    *,
    session: Session,
    tenant: Any,
    interaction: Any,
    features: Dict[str, Any],
    _loop: "Optional[_TaskEventLoop]" = None,
) -> None:
    """Build and persist a ``DeltaReport`` scoped to every touched entity.

    The LLM call here is a small Sonnet invocation producing ≤1k tokens of
    structured JSON.  Failure is logged but never fatal — the orchestrator
    can still run without a delta, it just has less evidence.
    """
    from backend.app.services.orchestrator import (
        DeltaReportWriter,
        EntityScope,
        ENTITY_AGENT,
        ENTITY_BUSINESS,
        ENTITY_CLIENT,
        ENTITY_MANAGER,
        get_orchestrator,
    )
    from backend.app.models import User

    scopes: List[EntityScope] = [
        EntityScope(entity_type=ENTITY_BUSINESS, entity_id=str(tenant.id)),
    ]
    if interaction.contact_id:
        scopes.append(EntityScope(entity_type=ENTITY_CLIENT, entity_id=str(interaction.contact_id)))
    if interaction.agent_id:
        scopes.append(EntityScope(entity_type=ENTITY_AGENT, entity_id=str(interaction.agent_id)))
        agent = session.query(User).filter(User.id == interaction.agent_id).first()
        manager_id = getattr(agent, "manager_id", None) if agent else None
        if manager_id:
            scopes.append(EntityScope(entity_type=ENTITY_MANAGER, entity_id=str(manager_id)))

    writer = DeltaReportWriter()
    _coro = writer.write(
        tenant=tenant, interaction=interaction, features=features, scopes=scopes
    )
    delta = _loop.run(_coro) if _loop is not None else asyncio.run(_coro)
    if delta:
        get_orchestrator().record_delta(
            session,
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            scopes=scopes,
            delta=delta,
        )


def _emit_webhooks_for_interaction(
    tenant_id,
    interaction_id,
    insights: Dict[str, Any],
    outcome_type: Optional[str],
    outcome_confidence: Optional[float],
) -> None:
    """Fan out webhook events for a freshly analyzed interaction.

    Called from inside the sync Celery task, so we hop into an async
    session via ``asyncio.run``. Never blocks on HTTP — ``emit_event``
    writes delivery rows and enqueues the delivery task.
    """
    from backend.app.db import async_session
    from backend.app.services.webhook_dispatcher import emit_event

    summary = {
        "interaction_id": str(interaction_id),
        "summary": (insights or {}).get("summary", "")[:600],
        "sentiment_overall": (insights or {}).get("sentiment_overall"),
        "sentiment_score": (insights or {}).get("sentiment_score"),
        "churn_risk_signal": (insights or {}).get("churn_risk_signal"),
        "upsell_signal": (insights or {}).get("upsell_signal"),
    }

    async def _runner() -> None:
        async with async_session() as db:
            await emit_event(db, tenant_id, "interaction.analyzed", summary)
            if outcome_type:
                await emit_event(
                    db,
                    tenant_id,
                    "interaction.outcome_inferred",
                    {
                        **summary,
                        "outcome_type": outcome_type,
                        "outcome_confidence": outcome_confidence,
                    },
                )

    try:
        asyncio.run(_runner())
    except Exception:
        logger.exception(
            "Webhook emission failed for interaction %s", interaction_id
        )


# ── Celery Tasks ─────────────────────────────────────────────────────────


@celery_app.task(bind=True, name="process_voice_interaction", max_retries=3)
def process_voice_interaction(self, interaction_id: str) -> Dict[str, Any]:
    """Full batch pipeline for a voice upload.

    Steps:
    1.  Load interaction from DB
    2.  Load tenant config
    3.  Load audio file
    4.  Transcribe audio → segments
    5.  PII redaction (if enabled)
    6.  Call metrics
    7.  Compress transcript for LLM
    8.  Triage — complexity scoring
    9.  AI analysis (Haiku or Sonnet based on triage)
    10. Scorecard scoring
    11. Snippet identification
    12. Search indexing
    13. Update interaction row
    14. Insert action items
    15. Insert interaction scores
    16. Insert interaction snippets
    17. Fire outbound webhooks
    """
    from backend.app.models import Interaction, Tenant

    logger.info("Starting voice pipeline for interaction %s", interaction_id)
    session = _get_sync_session()

    try:
        # ── Step 1: Load interaction ─────────────────────────────────
        interaction = (
            session.query(Interaction)
            .filter(Interaction.id == uuid.UUID(interaction_id))
            .first()
        )
        if interaction is None:
            logger.error("Interaction %s not found", interaction_id)
            return {"status": "error", "detail": "Interaction not found"}

        # ── Step 2: Load tenant config ───────────────────────────────
        tenant = (
            session.query(Tenant)
            .filter(Tenant.id == interaction.tenant_id)
            .first()
        )
        if tenant is None:
            logger.error("Tenant not found for interaction %s", interaction_id)
            return {"status": "error", "detail": "Tenant not found"}

        # ── Steps 3–4: Transcription ─────────────────────────────────
        # Three paths:
        #   (a) live-call interactions already have ``transcript`` filled
        #       by the Media Streams WebSocket handler → use as-is.
        #   (b) direct uploads landed their bytes in S3 staging under
        #       ``audio_s3_key`` → download, transcribe, delete.
        #   (c) external recording systems pushed us a pointer →
        #       ``audio_url`` is set → stream that URL directly to
        #       Deepgram (we never touch the bytes).

        segments_dicts: Optional[List[Dict[str, Any]]] = None
        staged_key: Optional[str] = None
        staged_path: Optional[str] = None

        if interaction.transcript and len(interaction.transcript) > 0:
            segments_dicts = interaction.transcript
            logger.info(
                "Using pre-populated transcript (%d segments) for interaction %s",
                len(segments_dicts), interaction_id,
            )
        elif interaction.audio_s3_key or interaction.audio_url:
            from backend.app.services import s3_audio
            from backend.app.services.transcription import TranscriptionService

            engine = interaction.engine or tenant.transcription_engine or "deepgram"
            keyterms = getattr(tenant, "keyterm_boost_list", None) or None
            language = getattr(tenant, "transcription_language", None) or "en"
            # Tenants can opt to a cheaper Deepgram model (nova-2) for
            # routine support traffic via features_enabled["deepgram_model"].
            # Defaults to nova-3 when unset.
            tenant_features_for_engine = getattr(tenant, "features_enabled", None) or {}
            deepgram_model = tenant_features_for_engine.get("deepgram_model")
            svc = TranscriptionService()

            # Paralinguistic extraction needs a local file. In URL mode
            # we'd normally hand the URL to Deepgram without downloading,
            # but if the tenant opted into paralinguistics we need the
            # bytes on disk — so fetch once and reuse.
            tenant_features = getattr(tenant, "features_enabled", None) or {}
            want_paralinguistic = bool(
                tenant_features.get("paralinguistic_analysis", True)
            )

            try:
                if (
                    interaction.audio_url
                    and engine == "deepgram"
                    and not want_paralinguistic
                ):
                    # URL mode: Deepgram fetches directly, we never stage.
                    segments = asyncio.run(
                        svc.transcribe(
                            audio_url=interaction.audio_url,
                            engine="deepgram",
                            language=language,
                            keyterms=keyterms,
                            model=deepgram_model,
                        )
                    )
                else:
                    # Need a local path — from S3 staging, or download the
                    # URL first (Whisper + paralinguistic both want bytes).
                    if interaction.audio_s3_key:
                        staged_key = interaction.audio_s3_key
                        staged_path = s3_audio.download_to_tempfile(staged_key)
                    else:
                        import httpx
                        import tempfile

                        tmp = tempfile.NamedTemporaryFile(
                            prefix="linda-audio-", suffix=".bin", delete=False
                        )
                        try:
                            with httpx.Client(timeout=60.0) as client:
                                resp = client.get(interaction.audio_url)
                                resp.raise_for_status()
                                tmp.write(resp.content)
                        finally:
                            tmp.close()
                        staged_path = tmp.name

                    segments = asyncio.run(
                        svc.transcribe(
                            audio_path=staged_path,
                            engine=engine,
                            language=language,
                            keyterms=keyterms,
                            model=deepgram_model,
                        )
                    )
                segments_dicts = _segments_to_dicts(segments)
                interaction.transcript = segments_dicts
                # Persist duration_seconds from the last segment if not set.
                if not interaction.duration_seconds and segments:
                    interaction.duration_seconds = int(segments[-1].end)
                session.commit()
            except Exception:
                logger.exception(
                    "Transcription failed for interaction %s", interaction_id
                )
                interaction.status = "transcription_failed"
                session.commit()
                # Clean up staged bytes on transcription failure too —
                # the rest of the pipeline won't run.
                _cleanup_staged_audio(session, interaction, staged_path, staged_key)
                raise
        else:
            logger.warning(
                "Interaction %s has no transcript, audio_s3_key, or audio_url",
                interaction_id,
            )
            interaction.status = "transcription_pending"
            session.commit()
            return {
                "status": "transcription_pending",
                "detail": "No audio source available",
            }

        # ── Steps 5–17: Run shared pipeline ──────────────────────────
        try:
            _run_pipeline(
                session,
                interaction_id,
                segments_dicts,
                tenant,
                interaction,
                audio_path=staged_path,
            )
        finally:
            # Paralinguistic extraction has had its shot — evict the
            # audio bytes. Keeps us aligned with the no-retention
            # product policy.
            _cleanup_staged_audio(session, interaction, staged_path, staged_key)
            staged_path = None
            staged_key = None

        try:
            from backend.app.services.metrics import PIPELINE_RUNS

            PIPELINE_RUNS.labels(
                channel=interaction.channel or "unknown",
                status="success",
            ).inc()
        except Exception:
            pass
        return {"status": "analyzed", "interaction_id": interaction_id}

    except Exception as exc:
        session.rollback()
        logger.exception(
            "Voice pipeline failed for interaction %s", interaction_id
        )
        # Update status to failed AND surface the exception in
        # ``insights['error']`` so the API + dashboard can show what
        # actually broke. Without this the row just goes opaque.
        try:
            interaction = (
                session.query(Interaction)
                .filter(Interaction.id == uuid.UUID(interaction_id))
                .first()
            )
            if interaction:
                interaction.status = "failed"
                err_payload = {
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "step": "voice_pipeline",
                    "retry_count": self.request.retries,
                }
                # Preserve any prior insights instead of clobbering.
                merged = dict(interaction.insights or {})
                merged.update(err_payload)
                interaction.insights = merged
                session.commit()
        except Exception:
            logger.exception("Failed to update interaction status to 'failed'")
        raise self.retry(exc=exc, countdown=60)
    finally:
        session.close()


@celery_app.task(bind=True, name="process_text_interaction", max_retries=3)
def process_text_interaction(self, interaction_id: str) -> Dict[str, Any]:
    """Batch pipeline for a text-based interaction (email, transcript).

    Similar to :func:`process_voice_interaction` but skips audio download
    and transcription (steps 3–4).  Uses ``raw_text`` from the interaction
    directly, converting it into a single-segment transcript.
    """
    from backend.app.models import Interaction, Tenant

    logger.info("Starting text pipeline for interaction %s", interaction_id)
    session = _get_sync_session()

    try:
        # ── Step 1: Load interaction ─────────────────────────────────
        interaction = (
            session.query(Interaction)
            .filter(Interaction.id == uuid.UUID(interaction_id))
            .first()
        )
        if interaction is None:
            logger.error("Interaction %s not found", interaction_id)
            return {"status": "error", "detail": "Interaction not found"}

        # ── Step 2: Load tenant config ───────────────────────────────
        tenant = (
            session.query(Tenant)
            .filter(Tenant.id == interaction.tenant_id)
            .first()
        )
        if tenant is None:
            logger.error("Tenant not found for interaction %s", interaction_id)
            return {"status": "error", "detail": "Tenant not found"}

        # ── Build segments from raw_text ─────────────────────────────
        # For text channels there is no audio — parse speaker tags
        # ("REP:", "PROSPECT:", "Maria Chen:") into proper turns. Falls
        # back to a single segment when the source has no recognizable
        # tags so anything unstructured still flows through the pipeline.
        if interaction.transcript and len(interaction.transcript) > 0:
            segments_dicts = interaction.transcript
        elif interaction.raw_text:
            from backend.app.services.text_segmenter import segments_from_text

            # Release the DB connection back to the pool before this
            # call — segments_from_text may invoke a 30-60s Haiku LLM
            # round-trip for un-tagged inputs, and holding the
            # connection idle during that time triggers Neon's
            # server-side idle-cutoff (TCP keepalives at the OS layer
            # aren't propagated through Neon's proxy). Committing now
            # is safe — at this point we've only done READ queries.
            session.commit()
            segments_dicts = segments_from_text(
                interaction.raw_text,
                duration_seconds=interaction.duration_seconds,
            )
            if not segments_dicts:
                logger.error(
                    "Text segmenter returned empty for interaction %s",
                    interaction_id,
                )
                interaction.status = "failed"
                session.commit()
                return {"status": "error", "detail": "Empty raw_text"}
        else:
            logger.error(
                "No text content for interaction %s", interaction_id
            )
            interaction.status = "failed"
            session.commit()
            return {"status": "error", "detail": "No text content"}

        # ── Steps 5–17: Run shared pipeline ──────────────────────────
        _run_pipeline(session, interaction_id, segments_dicts, tenant, interaction)

        try:
            from backend.app.services.metrics import PIPELINE_RUNS

            PIPELINE_RUNS.labels(
                channel=interaction.channel or "unknown",
                status="success",
            ).inc()
        except Exception:
            pass
        return {"status": "analyzed", "interaction_id": interaction_id}

    except Exception as exc:
        session.rollback()
        logger.exception(
            "Text pipeline failed for interaction %s", interaction_id
        )
        try:
            interaction = (
                session.query(Interaction)
                .filter(Interaction.id == uuid.UUID(interaction_id))
                .first()
            )
            if interaction:
                interaction.status = "failed"
                err_payload = {
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "step": "text_pipeline",
                    "retry_count": self.request.retries,
                }
                merged = dict(interaction.insights or {})
                merged.update(err_payload)
                interaction.insights = merged
                session.commit()
        except Exception:
            logger.exception("Failed to update interaction status to 'failed'")
        raise self.retry(exc=exc, countdown=60)
    finally:
        session.close()


# ── Scheduled periodic tasks ─────────────────────────────────────────────


@celery_app.task(name="email_push_process_gmail", bind=True, max_retries=3)
def email_push_process_gmail(self, integration_id: str, new_history_id: str) -> Dict[str, Any]:
    """Diff Gmail history from the cursor forward and ingest new messages.

    Called by the Pub/Sub push endpoint.  Keeps the HTTP handler fast:
    all API calls + DB writes happen here.
    """
    import asyncio as _asyncio

    from backend.app.models import EmailSyncCursor, Integration, Tenant, User
    from backend.app.services.email_classifier import EmailClassifier
    from backend.app.services.email_ingest.ingest import ingest_email
    from backend.app.services.email_ingest.poller import _refresh_if_expired_sync
    from backend.app.services.email_ingest.push import fetch_gmail_since_history

    session = _get_sync_session()
    try:
        integration = (
            session.query(Integration)
            .filter(Integration.id == uuid.UUID(integration_id))
            .first()
        )
        if integration is None:
            return {"status": "integration_missing"}

        tenant = session.query(Tenant).filter(Tenant.id == integration.tenant_id).first()
        if tenant is None:
            return {"status": "tenant_missing"}

        user = (
            session.query(User).filter(User.id == integration.user_id).first()
            if integration.user_id else None
        )
        agent_email = user.email if user else None

        cursor = (
            session.query(EmailSyncCursor)
            .filter(EmailSyncCursor.integration_id == integration.id)
            .first()
        )
        if cursor is None:
            cursor = EmailSyncCursor(
                integration_id=integration.id,
                tenant_id=integration.tenant_id,
                provider="google",
            )
            session.add(cursor)
            session.flush()
        start_history = cursor.history_id or new_history_id

        access_token = _refresh_if_expired_sync(session, integration)
        classifier = EmailClassifier()
        ingested = 0

        async def _run():
            nonlocal ingested
            for msg in fetch_gmail_since_history(access_token, start_history, agent_email):
                if await ingest_email(session, tenant, msg, classifier) is not None:
                    ingested += 1

        _asyncio.run(_run())
        # Always move the cursor forward even when nothing ingested, so
        # an internal-only burst doesn't make us keep re-diffing it.
        cursor.history_id = new_history_id
        session.commit()
        return {"status": "ok", "ingested": ingested}

    except Exception as exc:
        session.rollback()
        logger.exception("Gmail push task failed")
        raise self.retry(exc=exc, countdown=30)
    finally:
        session.close()


@celery_app.task(name="email_push_process_graph", bind=True, max_retries=3)
def email_push_process_graph(
    self,
    integration_id: str,
    message_id: str,
    parent_folder_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a single Graph message and route it through the ingest path."""
    import asyncio as _asyncio

    from backend.app.models import Integration, Tenant, User
    from backend.app.services.email_classifier import EmailClassifier
    from backend.app.services.email_ingest.ingest import ingest_email
    from backend.app.services.email_ingest.poller import _refresh_if_expired_sync
    from backend.app.services.email_ingest.push import fetch_graph_message

    session = _get_sync_session()
    try:
        integration = (
            session.query(Integration)
            .filter(Integration.id == uuid.UUID(integration_id))
            .first()
        )
        if integration is None:
            return {"status": "integration_missing"}

        tenant = session.query(Tenant).filter(Tenant.id == integration.tenant_id).first()
        if tenant is None:
            return {"status": "tenant_missing"}

        user = (
            session.query(User).filter(User.id == integration.user_id).first()
            if integration.user_id else None
        )
        agent_email = user.email if user else None

        access_token = _refresh_if_expired_sync(session, integration)

        # Folder id hint from the notification is the fastest direction
        # signal; otherwise we infer from sender vs. agent email.
        direction_hint = None
        if parent_folder_id:
            lowered = parent_folder_id.lower()
            if "sent" in lowered:
                direction_hint = "outbound"
            elif "inbox" in lowered:
                direction_hint = "inbound"

        msg = fetch_graph_message(access_token, message_id, agent_email, direction_hint)
        if msg is None:
            session.commit()
            return {"status": "fetch_failed"}

        classifier = EmailClassifier()
        async def _run():
            return await ingest_email(session, tenant, msg, classifier)

        ingested_id = _asyncio.run(_run())
        session.commit()
        return {"status": "ok", "ingested": bool(ingested_id)}

    except Exception as exc:
        session.rollback()
        logger.exception("Graph push task failed")
        raise self.retry(exc=exc, countdown=30)
    finally:
        session.close()


@celery_app.task(name="email_push_renew_subscriptions")
def email_push_renew_subscriptions() -> Dict[str, Any]:
    """(Re-)register Gmail watches and Graph subscriptions.

    Runs on a 12h schedule.  Expired watches/subscriptions are simply
    recreated; the provider returns the same stream so we pick up
    wherever we left off.  Requires PUBLIC_WEBHOOK_BASE_URL /
    GMAIL_PUBSUB_TOPIC / GRAPH_CLIENT_STATE to be configured —
    otherwise the task no-ops.
    """
    from backend.app.models import EmailSyncCursor, Integration
    from backend.app.services.email_ingest.poller import _refresh_if_expired_sync
    from backend.app.services.email_ingest.push import (
        subscribe_graph_mailbox,
        watch_gmail,
    )

    s = get_settings()
    base_url = s.PUBLIC_WEBHOOK_BASE_URL.rstrip("/")
    if not base_url:
        logger.info("PUBLIC_WEBHOOK_BASE_URL unset — skipping push renewal")
        return {"status": "skipped", "reason": "no_public_url"}

    session = _get_sync_session()
    gmail_ok = graph_ok = failed = 0
    try:
        integrations = (
            session.query(Integration)
            .filter(Integration.provider.in_(["google", "microsoft"]))
            .all()
        )
        for integ in integrations:
            try:
                access_token = _refresh_if_expired_sync(session, integ)
            except Exception:
                failed += 1
                logger.exception("Refresh failed for integration %s", integ.id)
                continue

            cursor = (
                session.query(EmailSyncCursor)
                .filter(EmailSyncCursor.integration_id == integ.id)
                .first()
            )
            if cursor is None:
                cursor = EmailSyncCursor(
                    integration_id=integ.id,
                    tenant_id=integ.tenant_id,
                    provider=integ.provider,
                )
                session.add(cursor)
                session.flush()

            try:
                if integ.provider == "google" and s.GMAIL_PUBSUB_TOPIC:
                    resp = watch_gmail(access_token, s.GMAIL_PUBSUB_TOPIC)
                    # Persist the watch's historyId so the first push
                    # notification has something to diff against.
                    cursor.history_id = str(resp.get("historyId") or cursor.history_id or "")
                    gmail_ok += 1
                elif integ.provider == "microsoft" and s.GRAPH_CLIENT_STATE:
                    notification_url = (
                        f"{base_url}{s.API_V1_PREFIX}/email-push/graph"
                    )
                    resp = subscribe_graph_mailbox(
                        access_token,
                        notification_url=notification_url,
                        client_state=s.GRAPH_CLIENT_STATE,
                    )
                    # Reuse delta_link as a handle to the subscription id —
                    # the notification endpoint looks it up there.
                    cursor.delta_link = resp.get("id") or cursor.delta_link
                    graph_ok += 1
            except Exception:
                failed += 1
                logger.exception(
                    "Push subscription failed for integration %s (%s)",
                    integ.id, integ.provider,
                )
        session.commit()
    finally:
        session.close()

    return {
        "status": "ok",
        "gmail_subscribed": gmail_ok,
        "graph_subscribed": graph_ok,
        "failed": failed,
    }


@celery_app.task(name="email_ingest_poll")
def email_ingest_poll() -> Dict[str, Any]:
    """Poll every connected Google/Microsoft integration for new mail.

    Scheduled every 2 minutes by Celery Beat.  Each integration advances
    its own ``EmailSyncCursor`` so we only fetch deltas.  External,
    customer-facing emails are created as ``Interaction(channel='email')``
    rows and enqueued for the standard text-analysis pipeline.  Internal
    emails are dropped with a log line and never touch the Interaction
    table.
    """
    from backend.app.services.email_ingest.poller import poll_all

    session = _get_sync_session()
    try:
        return poll_all(session)
    finally:
        session.close()


@celery_app.task(name="tenant_insights_weekly")
def tenant_insights_weekly() -> Dict[str, Any]:
    """Weekly rollup of tenant-level insights.

    Writes/updates a ``TenantInsight`` row per tenant for the last 7 days.
    Triggered by Celery Beat (see ``beat_schedule`` above).
    """
    from backend.app.services.tenant_insights_service import rollup_all_tenants_weekly

    session = _get_sync_session()
    try:
        processed = rollup_all_tenants_weekly(session)
        return {"tenants_processed": processed}
    finally:
        session.close()


# ── Orchestrator Celery tasks ────────────────────────────────────────────


@celery_app.task(name="_orchestrate_one_tenant")
def _orchestrate_one_tenant(tenant_id: str) -> Dict[str, Any]:
    """Per-tenant body of the daily orchestrator. Runs in its own worker
    slot so the parent fan-out can parallelize across the fleet.

    Returns a dict the chord callback can reduce — one tenant's failure
    surfaces as ``success=False`` rather than aborting the whole run.
    """
    from backend.app.models import Tenant
    from backend.app.services.orchestrator import get_orchestrator

    session = _get_sync_session()
    try:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            return {"tenant_id": tenant_id, "success": False, "error": "missing"}
        orch = get_orchestrator()
        counts: Dict[str, int] = {}
        baseline_refreshed = False
        try:
            counts = orch.run_daily(session, tenant.id) or {}
        except Exception as exc:  # noqa: BLE001 — per-tenant isolation
            logger.exception(
                "Daily orchestrator failed for tenant %s", tenant_id
            )
            return {
                "tenant_id": str(tenant_id),
                "success": False,
                "totals": {},
                "baseline_refreshed": False,
                "error": repr(exc),
            }
        try:
            baseline_refreshed = bool(
                _refresh_paralinguistic_baselines(session, tenant)
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Paralinguistic baseline refresh failed for tenant %s",
                tenant_id,
            )
        return {
            "tenant_id": str(tenant_id),
            "success": True,
            "totals": dict(counts),
            "baseline_refreshed": baseline_refreshed,
        }
    finally:
        session.close()


@celery_app.task(name="_aggregate_orchestration")
def _aggregate_orchestration(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce per-tenant orchestration results back into the original
    aggregate-shape return contract used by ``orchestrator_daily_all_tenants``."""
    totals: Dict[str, int] = {}
    baselines_refreshed = 0
    processed = 0
    failed: List[str] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        if r.get("success"):
            processed += 1
            for k, v in (r.get("totals") or {}).items():
                totals[k] = totals.get(k, 0) + int(v)
            if r.get("baseline_refreshed"):
                baselines_refreshed += 1
        else:
            failed.append(str(r.get("tenant_id") or "unknown"))
    return {
        "tenants_processed": processed,
        "profile_updates": totals,
        "paralinguistic_baselines_refreshed": baselines_refreshed,
        "failed_tenants": failed,
    }


@celery_app.task(name="orchestrator_daily_all_tenants")
def orchestrator_daily_all_tenants() -> Dict[str, Any]:
    """Daily consolidation of delta reports into profile versions.

    Fans out one task per tenant via a Celery chord so per-tenant work
    runs in parallel across workers. The callback reduces the per-tenant
    results back into the aggregate shape the beat-schedule consumer
    expects (tenants_processed, profile_updates, paralinguistic_baselines_refreshed).

    Failure semantics: a single tenant's exception no longer halts the
    rest — it surfaces in ``failed_tenants`` instead.
    """
    from celery import chord, group

    from backend.app.models import Tenant

    session = _get_sync_session()
    try:
        tenant_ids = [str(t.id) for t in session.query(Tenant.id).all()]
    finally:
        session.close()

    if not tenant_ids:
        return {
            "tenants_processed": 0,
            "profile_updates": {},
            "paralinguistic_baselines_refreshed": 0,
            "failed_tenants": [],
        }

    header = group(_orchestrate_one_tenant.s(tid) for tid in tenant_ids)
    async_result = chord(header)(_aggregate_orchestration.s())
    # Block on the chord so the beat consumer still gets the aggregate
    # dict it always returned. Timeout = generous; per-tenant tasks each
    # have their own internal try/except, so the chord only stalls when
    # workers are saturated.
    try:
        return async_result.get(disable_sync_subtasks=False, timeout=3600)
    except Exception:  # noqa: BLE001
        logger.exception(
            "orchestrator chord aggregation failed; returning partial state"
        )
        return {
            "tenants_processed": 0,
            "profile_updates": {},
            "paralinguistic_baselines_refreshed": 0,
            "failed_tenants": tenant_ids,
        }


def _refresh_paralinguistic_baselines(session: Session, tenant: Any) -> bool:
    """Recompute per-tenant acoustic percentiles off the last 90 days of
    interactions and persist them on ``Tenant.paralinguistic_baselines``.

    Returns True when the tenant had enough paralinguistic-enabled
    interactions (≥10) to compute meaningful baselines, False otherwise.
    """
    from backend.app.models import Interaction, InteractionFeatures
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(days=90)
    rows = (
        session.query(InteractionFeatures.deterministic)
        .join(Interaction, Interaction.id == InteractionFeatures.interaction_id)
        .filter(
            Interaction.tenant_id == tenant.id,
            Interaction.channel == "voice",
            Interaction.created_at >= cutoff,
        )
        .all()
    )
    customer_db: List[float] = []
    agent_pitch_std: List[float] = []
    for (det,) in rows:
        block = ((det or {}).get("paralinguistic") or {})
        if not block or not block.get("available"):
            continue
        per_speaker = block.get("per_speaker") or {}
        agent = per_speaker.get("agent") or next(iter(per_speaker.values()), {}) or {}
        customer = per_speaker.get("customer")
        if customer is None and len(per_speaker) > 1:
            customer = list(per_speaker.values())[1]
        customer = customer or {}
        if (ps := agent.get("pitch_std_semitones")) is not None:
            agent_pitch_std.append(float(ps))
        if (cd := customer.get("intensity_db_p50")) is not None:
            customer_db.append(float(cd))

    if len(customer_db) < 10 and len(agent_pitch_std) < 10:
        return False

    def _pctile(values: List[float], p: float) -> Optional[float]:
        if not values:
            return None
        clean = sorted(values)
        idx = p * (len(clean) - 1)
        lo = int(idx)
        hi = min(len(clean) - 1, lo + 1)
        frac = idx - lo
        return round(clean[lo] + (clean[hi] - clean[lo]) * frac, 3)

    baselines = {
        "customer_intensity_db_p90": _pctile(customer_db, 0.9),
        "customer_intensity_db_p50": _pctile(customer_db, 0.5),
        "agent_pitch_std_semitones_p50": _pctile(agent_pitch_std, 0.5),
        "sample_counts": {
            "customer_intensity": len(customer_db),
            "agent_pitch_std": len(agent_pitch_std),
        },
        "computed_at": datetime.utcnow().isoformat(),
    }
    tenant.paralinguistic_baselines = baselines
    session.commit()
    return True


@celery_app.task(name="orchestrator_weekly_all_tenants")
def orchestrator_weekly_all_tenants() -> Dict[str, Any]:
    """Weekly self-improvement reflection across all tenants."""
    from backend.app.models import Tenant
    from backend.app.services.orchestrator import get_orchestrator

    session = _get_sync_session()
    orch = get_orchestrator()
    results: Dict[str, Any] = {}
    try:
        for tenant in session.query(Tenant).all():
            try:
                results[str(tenant.id)] = orch.run_weekly(session, tenant.id)
            except Exception:
                logger.exception(
                    "Weekly orchestrator failed for tenant %s", tenant.id
                )
    finally:
        session.close()
    return {"tenants_processed": len(results)}


# ── Outcomes backfill & calibration ──────────────────────────────────────


@celery_app.task(name="outcomes_backfill_all_tenants")
def outcomes_backfill_all_tenants() -> Dict[str, Any]:
    """Backfill proxy outcomes from internal signals across all tenants."""
    from backend.app.models import Tenant
    from backend.app.services.outcomes_backfill import run_all

    session = _get_sync_session()
    totals: Dict[str, int] = {}
    tenants_done = 0
    try:
        for tenant in session.query(Tenant).all():
            try:
                counts = run_all(session, tenant.id)
                for k, v in counts.items():
                    totals[k] = totals.get(k, 0) + v
                tenants_done += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Outcome backfill failed for tenant %s", tenant.id
                )
    finally:
        session.close()
    return {"tenants_processed": tenants_done, "writes": totals}


@celery_app.task(name="calibration_fit_all_tenants")
def calibration_fit_all_tenants() -> Dict[str, Any]:
    """Refit Platt scaling for every configured scorer, per tenant."""
    from backend.app.models import Tenant
    from backend.app.services.calibration import fit_all_scorers

    session = _get_sync_session()
    activated = 0
    skipped = 0
    try:
        for tenant in session.query(Tenant).all():
            try:
                results = fit_all_scorers(session, tenant.id)
                for r in results:
                    if r.activated:
                        activated += 1
                    else:
                        skipped += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Calibration failed for tenant %s", tenant.id
                )
    finally:
        session.close()
    return {"activated": activated, "skipped": skipped}


@celery_app.task(name="irt_fit_all_tenants")
def irt_fit_all_tenants() -> Dict[str, Any]:
    """Weekly IRT fit across every tenant's scorecard templates."""
    from backend.app.models import Tenant
    from backend.app.services.irt import fit_all_templates_for_tenant

    session = _get_sync_session()
    summary: Dict[str, int] = {"templates_fit": 0, "items_fit": 0, "items_retired": 0}
    try:
        for tenant in session.query(Tenant).all():
            try:
                results = fit_all_templates_for_tenant(session, tenant.id)
                summary["templates_fit"] += len(results)
                for r in results:
                    summary["items_fit"] += r.n_items_fitted
                    summary["items_retired"] += len(r.retired_items)
            except Exception:  # noqa: BLE001
                logger.exception("IRT fit failed for tenant %s", tenant.id)
    finally:
        session.close()
    return summary


@celery_app.task(name="churn_train_all_tenants")
def churn_train_all_tenants() -> Dict[str, Any]:
    """Weekly Cox churn-model training; silently no-ops when data is thin."""
    from backend.app.models import Tenant
    from backend.app.services.churn_model import train_for_tenant

    session = _get_sync_session()
    summary = {"trained": 0, "insufficient_data": 0}
    try:
        for tenant in session.query(Tenant).all():
            try:
                result = train_for_tenant(session, tenant.id)
                if result.status == "ok":
                    summary["trained"] += 1
                else:
                    summary["insufficient_data"] += 1
            except Exception:  # noqa: BLE001
                logger.exception("Churn training failed for tenant %s", tenant.id)
    finally:
        session.close()
    return summary
# ── Continuous AI improvement tasks ──────────────────────────────────────


@celery_app.task(name="consume_feedback_stream")
def consume_feedback_stream() -> Dict[str, Any]:
    """Drain the Redis feedback stream into ``feedback_events``.

    Idempotent and safe to run on a 30s cadence.  Returns number of events
    persisted in this batch.
    """
    from backend.app.services import feedback_service

    session = _get_sync_session()
    try:
        return feedback_service.consume_batch(session)
    finally:
        session.close()


@celery_app.task(name="evaluate_analysis", bind=True, max_retries=3)
def evaluate_analysis(self, interaction_id: str) -> Dict[str, Any]:
    """LLM-judge the analysis insights for an interaction.  Chained 15-min after the producer."""
    from backend.app.services.llm_judge import evaluate_analysis as run

    session = _get_sync_session()
    try:
        return run(session, interaction_id)
    except Exception as exc:
        logger.exception("evaluate_analysis failed for %s", interaction_id)
        raise self.retry(exc=exc, countdown=300)
    finally:
        session.close()


@celery_app.task(name="evaluate_classification", bind=True, max_retries=3)
def evaluate_classification(self, interaction_id: str) -> Dict[str, Any]:
    """LLM-judge an email classification verdict."""
    from backend.app.services.llm_judge import evaluate_classification as run

    session = _get_sync_session()
    try:
        return run(session, interaction_id)
    except Exception as exc:
        logger.exception("evaluate_classification failed for %s", interaction_id)
        raise self.retry(exc=exc, countdown=300)
    finally:
        session.close()


@celery_app.task(name="evaluate_reply", bind=True, max_retries=3)
def evaluate_reply(self, interaction_id: str) -> Dict[str, Any]:
    """LLM-judge an outbound email reply (5 LLM dimensions; edit-distance is sync)."""
    from backend.app.services.llm_judge import evaluate_reply as run

    session = _get_sync_session()
    try:
        return run(session, interaction_id)
    except Exception as exc:
        logger.exception("evaluate_reply failed for %s", interaction_id)
        raise self.retry(exc=exc, countdown=300)
    finally:
        session.close()


@celery_app.task(name="refresh_few_shot_pools")
def refresh_few_shot_pools() -> Dict[str, Any]:
    """Promote high-quality interactions into each tenant's few-shot pool."""
    from backend.app.services.personalization_service import refresh_pools_all_tenants

    session = _get_sync_session()
    try:
        return refresh_pools_all_tenants(session)
    finally:
        session.close()


@celery_app.task(name="compute_wer_weekly")
def compute_wer_weekly() -> Dict[str, Any]:
    """Aggregate the prior 7 days of transcript_corrections into wer_metrics."""
    from backend.app.services.wer_service import compute_weekly

    session = _get_sync_session()
    try:
        return compute_weekly(session)
    finally:
        session.close()


@celery_app.task(name="discover_vocabulary_candidates")
def discover_vocabulary_candidates() -> Dict[str, Any]:
    """Surface new candidate keyterms from corrections + low-confidence segments."""
    from backend.app.services.vocabulary_service import discover_candidates_all_tenants

    session = _get_sync_session()
    try:
        return discover_candidates_all_tenants(session)
    finally:
        session.close()


@celery_app.task(name="cross_tenant_aggregate_metrics")
def cross_tenant_aggregate_metrics() -> Dict[str, Any]:
    """Compute opt-in cross-tenant aggregates (no tenant_id leakage)."""
    from backend.app.services.cross_tenant_metrics import aggregate_weekly

    session = _get_sync_session()
    try:
        return aggregate_weekly(session)
    finally:
        session.close()


@celery_app.task(name="quality_regression_check")
def quality_regression_check() -> Dict[str, Any]:
    """Watchdog: alert if 24h rolling quality drops > 5% vs. 7-day baseline."""
    from backend.app.services.regression_watchdog import check_all_active_rollouts

    session = _get_sync_session()
    try:
        return check_all_active_rollouts(session)
    finally:
        session.close()


@celery_app.task(name="variant_winner_selection")
def variant_winner_selection() -> Dict[str, Any]:
    """Promote / retire prompt variants based on accumulated quality scores."""
    from backend.app.services.variant_rollout import evaluate_active_experiments

    session = _get_sync_session()
    try:
        return evaluate_active_experiments(session)
    finally:
        session.close()


@celery_app.task(name="vocabulary_digest_weekly")
def vocabulary_digest_weekly() -> Dict[str, Any]:
    """Send the weekly Slack digest of pending vocabulary candidates."""
    from backend.app.services.digest_service import send_vocabulary_digests

    session = _get_sync_session()
    try:
        return send_vocabulary_digests(session)
    finally:
        session.close()


@celery_app.task(name="campaign_variant_winner_selection")
def campaign_variant_winner_selection() -> Dict[str, Any]:
    """Decide winners for active campaign A/B variants using engagement events."""
    from backend.app.services.campaign_winner_service import decide_active_campaigns

    session = _get_sync_session()
    try:
        return decide_active_campaigns(session)
    finally:
        session.close()


@celery_app.task(name="audio_retention_sweep")
def audio_retention_sweep() -> Dict[str, Any]:
    """Delete audio objects past their tenant's retention window.

    Tenant-agnostic — we scan the bucket and rely on the per-object
    ``retention_hours`` + ``stored_at`` tags set at upload time.  This
    means we honor per-tenant overrides even when the Tenant row's
    ``audio_retention_hours`` has changed since upload.
    """
    from backend.app.services.audio_storage import get_audio_store

    try:
        deleted = get_audio_store().sweep_expired()
        return {"deleted": deleted}
    except Exception:  # noqa: BLE001
        logger.exception("audio_retention_sweep failed")
        return {"deleted": 0, "error": True}


@celery_app.task(name="rebuild_tenant_context")
def rebuild_tenant_context(tenant_id: str, full: bool = False) -> Dict[str, Any]:
    """Rebuild LINDA's per-tenant company-context brief from the KB.

    Debounced via a Redis token key (see ``schedule_context_rebuild``) so a
    rapid flurry of KB uploads collapses into a single rebuild. If ``full`` is
    True, the builder streams every doc; otherwise it does an incremental
    merge on the most recent doc (populated in Redis by the caller).
    """
    from sqlalchemy import select as _select

    from backend.app.db import async_session
    from backend.app.models import KBDocument
    from backend.app.services.kb.context_builder import ContextBuilderService
    from backend.app.services.kb.context_dispatch import claim_debounce

    async def _runner() -> Dict[str, Any]:
        tid = uuid.UUID(tenant_id)

        # Honor the debounce: if someone bumped the timer forward while we
        # were asleep in the Celery queue, bail out — a fresh task is
        # already scheduled.
        if not full and not await claim_debounce(tid):
            return {"tenant_id": tenant_id, "skipped": "debounced"}

        builder = ContextBuilderService()
        async with async_session() as db:
            if full:
                brief = await builder.rebuild_all(db, tid)
                return {"tenant_id": tenant_id, "mode": "full", "brief_keys": list(brief.keys())}

            # Incremental: pick up the most recently updated doc for this
            # tenant and merge it in. A burst of uploads coalesces into this
            # single merge because the debounce key only fires once.
            stmt = (
                _select(KBDocument)
                .where(KBDocument.tenant_id == tid)
                .order_by(KBDocument.created_at.desc())
                .limit(1)
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return {"tenant_id": tenant_id, "mode": "incremental", "skipped": "no_docs"}
            brief = await builder.merge_document(db, tid, row)
            return {"tenant_id": tenant_id, "mode": "incremental", "brief_keys": list(brief.keys())}

    return asyncio.run(_runner())


@celery_app.task(name="rebuild_customer_brief")
def rebuild_customer_brief(tenant_id: str, customer_id: str) -> Dict[str, Any]:
    """Rebuild one customer's brief (debounced via Redis). Fired on
    interaction close, outcome log, and admin demand."""
    from backend.app.db import async_session
    from backend.app.services.kb.context_dispatch import claim_customer_debounce
    from backend.app.services.kb.customer_brief_builder import CustomerBriefBuilder

    async def _runner() -> Dict[str, Any]:
        cid = uuid.UUID(customer_id)
        if not await claim_customer_debounce(cid):
            return {"customer_id": customer_id, "skipped": "debounced"}
        builder = CustomerBriefBuilder()
        async with async_session() as db:
            brief = await builder.build(db, uuid.UUID(tenant_id), cid)
            return {
                "customer_id": customer_id,
                "status": brief.get("current_status"),
                "source_interaction_count": brief.get("source_interaction_count"),
            }

    return asyncio.run(_runner())


@celery_app.task(name="tenant_brief_refiner_weekly")
def tenant_brief_refiner_weekly(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Run the TenantBriefRefiner for one tenant (if tenant_id given) or all
    tenants. Invoked by Celery beat once a week, and also as a fan-out from
    admin-triggered refines."""
    from backend.app.db import async_session
    from backend.app.models import Tenant as _Tenant
    from backend.app.services.kb.tenant_brief_refiner import TenantBriefRefiner
    from sqlalchemy import select as _select

    async def _runner() -> Dict[str, Any]:
        refiner = TenantBriefRefiner()
        async with async_session() as db:
            if tenant_id:
                tids = [uuid.UUID(tenant_id)]
            else:
                rows = await db.execute(_select(_Tenant.id))
                tids = [uuid.UUID(str(r[0])) for r in rows.all()]

            results: List[Dict[str, Any]] = []
            for tid in tids:
                try:
                    pb = await refiner.refine(db, tid)
                    results.append({"tenant_id": str(tid), "sample_size": pb.get("sample_size")})
                except Exception:
                    logger.exception("TenantBriefRefiner failed for tenant %s", tid)
                    results.append({"tenant_id": str(tid), "error": True})
        return {"tenants_processed": len(results), "results": results}

    return asyncio.run(_runner())


@celery_app.task(name="infer_from_sources_weekly")
def infer_from_sources_weekly(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Run the Infer-From-Sources agent for one tenant or all tenants.

    Emits TenantBriefSuggestion rows for the tenant admin to review.
    Never auto-writes to the tenant brief.
    """
    from backend.app.db import async_session
    from backend.app.models import Tenant as _Tenant
    from backend.app.services.kb.infer_from_sources import InferFromSources
    from sqlalchemy import select as _select

    async def _runner() -> Dict[str, Any]:
        agent = InferFromSources()
        async with async_session() as db:
            if tenant_id:
                tids = [uuid.UUID(tenant_id)]
            else:
                rows = await db.execute(_select(_Tenant.id))
                tids = [uuid.UUID(str(r[0])) for r in rows.all()]

            results: List[Dict[str, Any]] = []
            for tid in tids:
                try:
                    new_rows = await agent.run(db, tid)
                    results.append(
                        {
                            "tenant_id": str(tid),
                            "new_suggestions": len(new_rows),
                        }
                    )
                except Exception:
                    logger.exception(
                        "InferFromSources failed for tenant %s", tid
                    )
                    results.append({"tenant_id": str(tid), "error": True})
            return {"tenants_processed": len(results), "results": results}

    return asyncio.run(_runner())


@celery_app.task(name="vector_health_daily")
def vector_health_daily() -> Dict[str, Any]:
    """Daily sustained-threshold check for the vector store.

    Uses the async engine via ``asyncio.run`` since the check touches Redis
    async APIs and is cheap to boot a loop for.
    """
    from backend.app.db import async_session
    from backend.app.services.kb.vector_health_check import run_vector_health_check

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            return await run_vector_health_check(db)

    return asyncio.run(_runner())


@celery_app.task(name="crm_writeback", max_retries=2)
def crm_writeback(interaction_id: str) -> Dict[str, Any]:
    """Apply CRM write-backs (notes, activities) for one interaction.

    Dispatched after the voice pipeline lands insights. Reads
    ``Tenant.features_enabled`` to decide which kinds of write-back to
    attempt; the tenant stays in control. Exceptions are swallowed by
    the write-back service so a CRM outage never poisons this task.
    """
    from backend.app.db import async_session
    from backend.app.services.crm.writeback import write_back_interaction

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            summary = await write_back_interaction(db, uuid.UUID(interaction_id))
            await db.commit()
            return summary

    return asyncio.run(_runner())


@celery_app.task(name="crm_sync_tenant")
def crm_sync_tenant(tenant_id: str, provider: str) -> Dict[str, Any]:
    """Run a single CRM sync for ``(tenant_id, provider)``."""
    from backend.app.db import async_session
    from backend.app.services.crm.sync_service import sync_crm_for_tenant

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            summary = await sync_crm_for_tenant(
                db, uuid.UUID(tenant_id), provider
            )
            return {
                "provider": summary.provider,
                "status": summary.status,
                "customers_upserted": summary.customers_upserted,
                "contacts_upserted": summary.contacts_upserted,
                "briefs_rebuilt": summary.briefs_rebuilt,
                "error": summary.error,
            }

    return asyncio.run(_runner())


@celery_app.task(name="crm_sync_daily")
def crm_sync_daily() -> Dict[str, Any]:
    """Nightly fan-out: for every Integration on a CRM provider, run a sync.

    Tenants without CRM integrations are silently skipped. A provider that
    returns ``not implemented`` (e.g. the Pipedrive stub) is counted as
    skipped rather than failed.
    """
    from sqlalchemy import select as _select

    from backend.app.db import async_session
    from backend.app.models import Integration
    from backend.app.services.crm.sync_service import (
        SUPPORTED_PROVIDERS,
        sync_crm_for_tenant,
    )

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            stmt = _select(
                Integration.tenant_id, Integration.provider
            ).where(Integration.provider.in_(list(SUPPORTED_PROVIDERS)))
            rows = await db.execute(stmt)
            pairs = {
                (uuid.UUID(str(t)), p) for (t, p) in rows.all()
            }

            results: List[Dict[str, Any]] = []
            for tenant_id, provider in pairs:
                try:
                    summary = await sync_crm_for_tenant(db, tenant_id, provider)
                    results.append(
                        {
                            "tenant_id": str(tenant_id),
                            "provider": provider,
                            "status": summary.status,
                            "customers": summary.customers_upserted,
                            "contacts": summary.contacts_upserted,
                        }
                    )
                except Exception:
                    logger.exception(
                        "CRM sync failed for tenant=%s provider=%s",
                        tenant_id,
                        provider,
                    )
                    results.append(
                        {
                            "tenant_id": str(tenant_id),
                            "provider": provider,
                            "status": "failed",
                        }
                    )
        return {"runs": results, "count": len(results)}

    return asyncio.run(_runner())


@celery_app.task(name="tenant_export_to_s3", max_retries=1)
def tenant_export_to_s3(
    tenant_id: str,
    s3_key_prefix: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce an NDJSON GDPR export for ``tenant_id`` and upload it to
    the tenant-owned S3 bucket.

    This is the non-interactive companion to ``GET /tenants/{id}/export``
    — suitable for scheduled backups or an admin-triggered async
    export the UI polls for. Writes to
    ``{AWS_S3_BUCKET}/{s3_key_prefix}/{timestamp}.ndjson.gz`` by default.

    The bundle is gzipped on the fly so terabyte-scale tenants don't
    pay 10x storage for JSON boilerplate. Metadata (row counts per
    table, schema version) lands in the accompanying ``.meta.json``.
    """
    import gzip
    import io
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    from backend.app.db import async_session
    from backend.app.services import s3_audio
    from backend.app.services.tenant_dataops import export_tenant

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            buf = io.BytesIO()
            line_count = 0
            tenant_uuid = _uuid.UUID(tenant_id)
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                async for chunk in export_tenant(db, tenant_uuid):
                    gz.write(chunk)
                    line_count += 1
            body = buf.getvalue()

        prefix = (s3_key_prefix or "backups").strip("/")
        timestamp = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S")
        key = f"{prefix}/{tenant_id}/{timestamp}.ndjson.gz"
        s3_audio.upload_bytes(
            tenant_id=tenant_uuid,
            recording_id=_uuid.UUID(tenant_id),
            data=body,
            content_type="application/gzip",
            s3_key_override=key,
        )
        logger.info(
            "Tenant export uploaded: tenant=%s key=%s bytes=%d lines=%d",
            tenant_id, key, len(body), line_count,
        )
        return {
            "tenant_id": tenant_id,
            "s3_key": key,
            "bytes": len(body),
            "lines": line_count,
            "reason": reason,
            "exported_at": timestamp,
        }

    return asyncio.run(_runner())


@celery_app.task(name="tenant_backup_all_tenants")
def tenant_backup_all_tenants() -> Dict[str, Any]:
    """Nightly backup fan-out — one export per tenant.

    Dispatches ``tenant_export_to_s3`` per tenant so one slow export
    doesn't hold up the others. Beat schedule below wires this to run
    daily; disable per-tenant by setting
    ``tenants.features_enabled['scheduled_backups'] = False``.
    """
    from backend.app.models import Tenant

    session = _get_sync_session()
    try:
        dispatched = 0
        skipped = 0
        for tenant in session.query(Tenant).all():
            feats = (tenant.features_enabled or {})
            if feats.get("scheduled_backups") is False:
                skipped += 1
                continue
            try:
                tenant_export_to_s3.delay(str(tenant.id), reason="nightly_backup")
                dispatched += 1
            except Exception:
                logger.exception(
                    "Failed to dispatch backup for tenant %s", tenant.id
                )
        return {"dispatched": dispatched, "skipped": skipped}
    finally:
        session.close()


@celery_app.task(name="tenant_restore_from_s3")
def tenant_restore_from_s3(s3_key: str) -> Dict[str, Any]:
    """Restore a tenant export back into the database.

    Reads the NDJSON.gz bundle from S3, walks it line by line, and
    upserts every row into its destination table. Intended for two
    scenarios:

    1. **Disaster recovery** — a tenant's data got corrupted and we
       need yesterday's backup back.
    2. **Environment sync** — copy a tenant from prod to staging for
       repro work (make sure PII redaction ran before the snapshot).

    The operation is idempotent on the primary key (``ON CONFLICT DO
    UPDATE``), so running it twice is safe. Foreign-key ordering is
    honored because ``_tenant_tables_reverse_topo`` emits parents
    first during export — the restore walks the file in order.
    """
    import gzip
    import json as _json

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from backend.app.db import async_session
    from backend.app.models import Base
    from backend.app.services import s3_audio

    async def _runner() -> Dict[str, Any]:
        blob = s3_audio.download_object_bytes(s3_key)  # type: ignore[attr-defined]
        data = gzip.decompress(blob).decode("utf-8")

        per_table_counts: Dict[str, int] = {}
        async with async_session() as db:
            for line in data.splitlines():
                if not line.strip():
                    continue
                try:
                    doc = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if doc.get("_meta") or doc.get("_eof"):
                    continue
                table_name = doc.get("_table")
                row = doc.get("row") or {}
                if not table_name or not row:
                    continue
                table = Base.metadata.tables.get(table_name)
                if table is None:
                    logger.warning("restore: skipping unknown table %s", table_name)
                    continue
                # Unwrap base64 blobs produced during export.
                row = {
                    k: (
                        __import__("base64").b64decode(v["__b64__"])
                        if isinstance(v, dict) and "__b64__" in v
                        else v
                    )
                    for k, v in row.items()
                }
                # Only populate columns the table actually has — a
                # restore from an older schema shouldn't fail on a
                # column that was dropped since.
                allowed = {c.name for c in table.columns}
                row = {k: v for k, v in row.items() if k in allowed}
                stmt = pg_insert(table).values(**row)
                pk_cols = [c.name for c in table.primary_key]
                if pk_cols:
                    stmt = stmt.on_conflict_do_update(
                        index_elements=pk_cols,
                        set_={
                            k: row[k] for k in row.keys() if k not in pk_cols
                        },
                    )
                await db.execute(stmt)
                per_table_counts[table_name] = (
                    per_table_counts.get(table_name, 0) + 1
                )
            await db.commit()
        return {"s3_key": s3_key, "restored": per_table_counts}

    return asyncio.run(_runner())


@celery_app.task(name="sync_knowledge_base")
def sync_knowledge_base(tenant_id: str, source_type: str) -> Dict[str, Any]:
    """Run one KB provider sync for a tenant.

    Dispatched by ``POST /kb/sync/{provider}`` and by the nightly
    scheduler (when we add one). Returns a summary the dispatcher can
    log / surface in the admin UI.
    """
    from backend.app.db import async_session
    from backend.app.services.kb.sync_runner import sync_kb_for_tenant

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            summary = await sync_kb_for_tenant(
                db, uuid.UUID(tenant_id), source_type
            )
            await db.commit()
            return {
                "source_type": summary.source_type,
                "status": summary.status,
                "docs_seen": summary.docs_seen,
                "docs_upserted": summary.docs_upserted,
                "chunks_written": summary.chunks_written,
                "error": summary.error,
            }

    return asyncio.run(_runner())


@celery_app.task(name="webhook_deliver")
def webhook_deliver(delivery_id: str) -> Dict[str, Any]:
    """Attempt one HTTP delivery for a WebhookDelivery row.

    The dispatcher re-enqueues retries via ``apply_async(countdown=...)``
    when it schedules the next attempt, so this task stays stateless.
    Tolerates the delivery row being gone (e.g., webhook deleted in the
    meantime) by returning status=missing.
    """
    from backend.app.db import async_session
    from backend.app.services.webhook_dispatcher import deliver_one

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            return await deliver_one(db, uuid.UUID(delivery_id))

    return asyncio.run(_runner())


# Celery queues we sample for backpressure. Default queue is ``celery``;
# add new queue names here when task routing is introduced. Do NOT scan
# the keyspace — on per-command-billed Redis (Upstash) a SCAN + TYPE on
# every key every 30 s dominates the bill.
_SAMPLED_CELERY_QUEUES: tuple[str, ...] = ("celery",)


@celery_app.task(name="sample_queue_depth")
def sample_queue_depth() -> Dict[str, int]:
    """Read each Celery queue's Redis LIST length into the
    ``linda_celery_queue_depth`` gauge.

    One LLEN per known queue every 30 s.
    """
    try:
        import redis

        from backend.app.services.metrics import CELERY_QUEUE_DEPTH

        redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        depths: Dict[str, int] = {}
        pipe = redis_client.pipeline(transaction=False)
        for queue in _SAMPLED_CELERY_QUEUES:
            pipe.llen(queue)
        results = pipe.execute()
        for queue, length in zip(_SAMPLED_CELERY_QUEUES, results):
            length_int = int(length or 0)
            depths[queue] = length_int
            CELERY_QUEUE_DEPTH.labels(queue=queue).set(length_int)
        return depths
    except Exception:
        logger.debug("queue depth sampling failed", exc_info=True)
        return {}


@celery_app.task(name="event_retention_sweep")
def event_retention_sweep() -> Dict[str, Any]:
    """Daily retention sweep for high-volume event tables.

    Drops ``webhook_deliveries`` older than 90 days (sent / dead-letter
    only — pending retries are preserved regardless of age). Rolls
    ``feedback_events`` older than 180 days into
    ``feedback_daily_rollup`` and deletes the raw rows so calibration
    can still see historical volume without paying raw-row storage.
    """
    from backend.app.db import async_session
    from backend.app.services.event_retention import run_event_retention_sweep

    async def _runner() -> Dict[str, Any]:
        async with async_session() as db:
            return await run_event_retention_sweep(db)

    return asyncio.run(_runner())


@celery_app.task(name="trial_expiry_daily")
def trial_expiry_daily() -> Dict[str, Any]:
    """Walk every sandbox tenant and act on the trial-end timeline.

    For each tenant on the ``sandbox`` tier with a non-NULL
    ``trial_ends_at``:

    * 3 / 1 days before ``trial_ends_at``: emit an "approaching" notice
      (logged + recorded in ``tenant_dataops_log``; if/when an email
      provider is wired the same code can switch to that transport).
    * On / past ``trial_ends_at``: emit an "expired" notice and flip
      ``Tenant.subscription_status`` to ``expired``. The
      ``require_active_subscription`` dependency already 402s
      revenue-burning endpoints once trial_ends_at is in the past, but
      the explicit status flag drives banners + reports.

    Idempotent: a tenant already at ``subscription_status="expired"``
    is skipped. Notices for any one (tenant, day-bucket) tuple are
    written through a unique reason key in tenant_dataops_log so a
    re-run of the same day doesn't double-log.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select

    from backend.app.db import async_session
    from backend.app.models import Tenant, TenantDataOpsLog

    async def _runner() -> Dict[str, Any]:
        emitted = {"warned_3d": 0, "warned_1d": 0, "expired": 0}
        now = datetime.now(timezone.utc)

        async with async_session() as db:
            stmt = select(Tenant).where(
                Tenant.plan_tier == "sandbox",
                Tenant.trial_ends_at.is_not(None),
            )
            tenants = list((await db.execute(stmt)).scalars().all())

            for tenant in tenants:
                ends = tenant.trial_ends_at
                if ends is None:
                    continue
                seconds_left = (ends - now).total_seconds()
                days_left = seconds_left / 86_400

                # Pick the first matching bucket. The thresholds are
                # left-closed: anything in (1, 3] days fires the 3d
                # bucket so the warning lands well before the 1d one.
                bucket: Optional[str] = None
                if seconds_left <= 0:
                    bucket = "expired"
                elif days_left <= 1:
                    bucket = "warned_1d"
                elif days_left <= 3:
                    bucket = "warned_3d"
                if bucket is None:
                    continue

                # Idempotency guard — skip if we already logged this
                # bucket today.
                day_key = now.strftime("%Y-%m-%d")
                reason_key = f"trial_{bucket}:{day_key}"
                already = (
                    await db.execute(
                        select(TenantDataOpsLog.id)
                        .where(
                            TenantDataOpsLog.tenant_id == tenant.id,
                            TenantDataOpsLog.operation == "trial_notice",
                            TenantDataOpsLog.reason == reason_key,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if already is not None:
                    continue

                if bucket == "expired":
                    if (
                        getattr(tenant, "subscription_status", None)
                        != "expired"
                    ):
                        tenant.subscription_status = "expired"
                logger.info(
                    "trial_expiry_daily: tenant=%s bucket=%s ends_at=%s",
                    tenant.id,
                    bucket,
                    ends.isoformat(),
                )
                db.add(
                    TenantDataOpsLog(
                        tenant_id=tenant.id,
                        operation="trial_notice",
                        status="success",
                        reason=reason_key,
                        counts={"days_left": round(days_left, 2)},
                    )
                )
                emitted[bucket] = emitted.get(bucket, 0) + 1

            await db.commit()
        return emitted

    return asyncio.run(_runner())


# ──────────────────────────────────────────────────────────
# Action Plan: inbound email matcher (Celery task)
# ──────────────────────────────────────────────────────────


@celery_app.task(
    name="action_plan_match_inbound_email", bind=True, max_retries=2,
)
def action_plan_match_inbound_email(
    self,
    interaction_id_str: str,
) -> Optional[str]:
    """For an inbound email Interaction, try to match it to an open
    Action Step via RFC 822 References + run Call D extraction.

    Returns the matched step_id (str) when one was found, None
    otherwise. Failures here NEVER bubble: we log and move on so a
    flaky LLM call doesn't kill email ingest.
    """

    async def _runner() -> Optional[str]:
        from backend.app.db import async_session as _async_session_factory
        from backend.app.models import ActionStep as _StepModel
        from backend.app.models import Interaction as _IxModel
        from backend.app.services.action_plan.engine import ActionPlanEngine
        from backend.app.services.action_plan.extractor import (
            ResponseExtractor,
            match_inbound_email,
        )

        async with _async_session_factory() as db:
            ix = await db.get(_IxModel, uuid.UUID(interaction_id_str))
            if ix is None or ix.channel != "email" or ix.direction != "inbound":
                return None
            match = await match_inbound_email(
                db,
                tenant_id=ix.tenant_id,
                in_reply_to=ix.in_reply_to,
                references=list(ix.references or []),
            )
            if match.step_id is None or match.reason in {"no_match", "step_closed"}:
                return None
            step = await db.get(_StepModel, match.step_id)
            if step is None:
                return None
            extractor = ResponseExtractor()
            body = ix.raw_text or ""
            extraction = await extractor.extract_for_step(
                step=step,
                source_label="inbound email",
                source_content=body,
            )
            from backend.app.models import StepResponse as _RespModel
            response = _RespModel(
                step_id=step.id,
                tenant_id=step.tenant_id,
                source="inbound_email",
                email_message_id=ix.id,
                extracted_data=extraction.extracted,
                source_quotes=extraction.source_quotes,
                unfilled_reasons=extraction.unfilled_reasons,
                extraction_confidence=extraction.confidence,
            )
            db.add(response)
            await db.flush()
            engine = ActionPlanEngine()
            await engine.apply_response(db, step=step, response=response)
            await db.commit()
            return str(step.id)

    try:
        return asyncio.run(_runner())
    except Exception:  # noqa: BLE001 - never let matching kill the task
        logger.exception(
            "action_plan_match_inbound_email failed for interaction %s "
            "(non-fatal)",
            interaction_id_str,
        )
        return None


@celery_app.task(
    name="action_plan_run_due_regenerations", bind=True,
)
def action_plan_run_due_regenerations(self) -> int:
    """Beat-scheduled tick that runs Call C for every step whose
    debounce timer has elapsed. Returns the count regenerated."""

    async def _runner() -> int:
        from backend.app.db import async_session as _async_session_factory
        from backend.app.services.action_plan.engine import (
            run_due_regenerations,
        )

        async with _async_session_factory() as db:
            count = await run_due_regenerations(db, limit=100)
            await db.commit()
            return count

    try:
        return asyncio.run(_runner())
    except Exception:
        logger.exception("action_plan_run_due_regenerations failed (non-fatal)")
        return 0


# ── Manager-view tasks (anomaly scan, recommendations, resolution) ────


@celery_app.task(name="manager_anomaly_scan_all_tenants")
def manager_anomaly_scan_all_tenants() -> Dict[str, Any]:
    """Run the three anomaly detectors for every tenant. Fans out alert
    delivery (in-app + Slack) inline. See
    ``backend.app.services.anomaly_detector``.
    """
    from backend.app.services.anomaly_detector import scan_all_tenants
    from backend.app.services.manager_alert_fanout import fanout

    session = _get_sync_session()
    try:
        result = scan_all_tenants(session)
        # Fanout: pull the freshly inserted alerts and deliver. The
        # detector already commits; here we re-load only those that
        # were created in the last 60 seconds to avoid double-delivery.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        from sqlalchemy import select as _select

        from backend.app.models import ManagerAlert

        cutoff = _dt.now(_tz.utc) - _td(seconds=60)
        fresh = (
            session.execute(
                _select(ManagerAlert).where(ManagerAlert.created_at >= cutoff)
            )
            .scalars()
            .all()
        )
        fanout(session, fresh)
        session.commit()
        return result
    finally:
        session.close()


@celery_app.task(name="manager_recommendations_build")
def manager_recommendations_build() -> Dict[str, Any]:
    """Daily Haiku-driven recommendation refresh per tenant."""
    from backend.app.services.manager_recommendation_builder import (
        build_for_all_tenants,
    )

    session = _get_sync_session()
    try:
        return build_for_all_tenants(session)
    finally:
        session.close()


@celery_app.task(name="manager_recommendations_expire")
def manager_recommendations_expire() -> Dict[str, Any]:
    """Sweep recommendations whose ``expires_at`` has passed."""
    from backend.app.services.manager_recommendation_builder import expire_old

    session = _get_sync_session()
    try:
        expired = expire_old(session)
        return {"expired": expired}
    finally:
        session.close()


@celery_app.task(name="manager_anomaly_resolve")
def manager_anomaly_resolve() -> Dict[str, Any]:
    """Mark stale manager_alerts whose underlying spike has subsided."""
    from backend.app.services.anomaly_detector import resolve_stale

    session = _get_sync_session()
    try:
        resolved = resolve_stale(session)
        return {"resolved": resolved}
    finally:
        session.close()


@celery_app.task(name="orchestrator_daily_one_tenant")
def orchestrator_daily_one_tenant(tenant_id: str) -> Dict[str, Any]:
    """Force-refresh one tenant's BusinessProfile + sibling profiles.

    Called from the manager-page "Refresh now" CTA. Rate-limited at the
    API layer (1/hr/tenant via Redis) to bound Opus cost. Internally
    just delegates to ``_orchestrate_one_tenant`` — the same per-tenant
    body the daily chord uses.
    """
    return _orchestrate_one_tenant(tenant_id)
