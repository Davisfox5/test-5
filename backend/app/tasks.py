"""Celery worker and task definitions — batch processing pipeline.

All async service calls are wrapped with ``asyncio.run()`` because Celery
tasks execute synchronously.  Database access uses a synchronous
SQLAlchemy session created via :func:`_get_sync_session`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# ── Celery app ───────────────────────────────────────────────────────────

celery_app = Celery(
    "linda",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
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
    },
)

# ── Synchronous SQLAlchemy session for Celery tasks ──────────────────────

_sync_db_url = settings.DATABASE_URL
# Ensure we use the synchronous driver (psycopg2) rather than asyncpg.
if _sync_db_url.startswith("postgresql+asyncpg://"):
    _sync_db_url = _sync_db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
elif _sync_db_url.startswith("postgres://"):
    pass  # already sync-compatible

_sync_engine = create_engine(
    _sync_db_url,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
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
    pii_redacted = False
    if tenant.pii_redaction_enabled:
        pii_config = tenant.pii_redaction_config or {}
        segments_dicts = _get_pii_service().redact_segments(
            segments_dicts, config=pii_config
        )
        pii_redacted = True
        logger.info("PII redaction complete for interaction %s", interaction_id)

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
    triage_result: Dict[str, Any] = _loop.run(
        _get_triage_service().score_complexity(compressed_text, metadata)
    )
    complexity_score = float(triage_result.get("complexity_score", 0.5))
    recommended_tier = triage_result.get("recommended_tier", "sonnet")
    logger.info(
        "Triage complete for interaction %s: score=%.2f tier=%s",
        interaction_id, complexity_score, recommended_tier,
    )

    # ── Step 9: AI analysis ──────────────────────────────────────────
    # Prompt-variant routing + personalization blocks.
    from backend.app.services.ai_analysis import ANALYSIS_SYSTEM_PROMPT
    from backend.app.services.personalization_service import (
        build_analysis_context_block,
        build_rag_context_block,
        get_parameter_overrides,
    )
    from backend.app.services.prompt_variant_service import (
        select_variant_sync,
        to_uuid as _variant_to_uuid,
    )

    variant = select_variant_sync(
        session,
        tenant,
        surface="analysis",
        tier=recommended_tier,
        channel=interaction.channel,
        fallback_template=ANALYSIS_SYSTEM_PROMPT,
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

    # ── Step 13: Update interaction row ──────────────────────────────
    interaction.status = "analyzed"
    interaction.transcript = segments_dicts
    interaction.insights = insights
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
    for ai_item in insights.get("action_items", []):
        action = ActionItem(
            interaction_id=interaction.id,
            tenant_id=tenant.id,
            title=ai_item.get("title", "Untitled"),
            description=ai_item.get("description", ""),
            category=ai_item.get("category"),
            priority=ai_item.get("priority", "medium"),
            status="pending",
        )
        session.add(action)

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
            start_time=float(sn.get("start_time", 0) or 0),
            end_time=float(sn.get("end_time", 0) or 0),
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

    # ── Step 17a: Paralinguistic features ────────────────────────────
    # Runs whenever we still have the audio file on disk (``audio_path``
    # is populated by process_voice_interaction for S3-staged uploads
    # and for URL-mode ingests when the tenant has opted in). Per-
    # speaker + overall acoustic features are stored under the
    # ``paralinguistic`` key so scorers can read them.
    tenant_features = getattr(tenant, "features_enabled", None) or {}
    if audio_path and tenant_features.get("paralinguistic_analysis", True):
        try:
            from backend.app.services.paralinguistics import (
                SpeakerAudioSegment,
                get_paralinguistic_extractor,
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
                deterministic_features["paralinguistic"] = para.as_dict()
        except Exception:
            logger.exception(
                "Paralinguistic extraction raised for %s (non-fatal)", interaction_id
            )

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

        return {"status": "analyzed", "interaction_id": interaction_id}

    except Exception as exc:
        session.rollback()
        logger.exception(
            "Voice pipeline failed for interaction %s", interaction_id
        )
        # Update status to failed.
        try:
            interaction = (
                session.query(Interaction)
                .filter(Interaction.id == uuid.UUID(interaction_id))
                .first()
            )
            if interaction:
                interaction.status = "failed"
                session.commit()
        except Exception:
            logger.exception("Failed to update interaction status to 'failed'")
        raise self.retry(exc=exc, countdown=60)
    finally:
        session.close()


@celery_app.task(bind=True, name="process_text_interaction", max_retries=3)
def process_text_interaction(self, interaction_id: str) -> Dict[str, Any]:
    """Batch pipeline for a text-based interaction (email, chat).

    Similar to :func:`process_voice_interaction` but skips audio download
    and transcription (steps 3–4).  Uses ``raw_text`` from the interaction
    directly, converting it into a single-segment transcript.

    SMS/WhatsApp paths are stubbed (see services/sms_ingest.py) but this
    function remains channel-agnostic — if those channels are re-enabled
    they'll flow through here unchanged.
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
        # For text channels there is no audio — use raw_text or
        # pre-existing transcript segments directly.
        if interaction.transcript and len(interaction.transcript) > 0:
            segments_dicts = interaction.transcript
        elif interaction.raw_text:
            # Wrap raw_text in a single segment for uniform processing.
            segments_dicts = [
                {
                    "start": 0.0,
                    "end": 0.0,
                    "text": interaction.raw_text,
                    "speaker_id": None,
                    "confidence": None,
                }
            ]
        else:
            logger.error(
                "No text content for interaction %s", interaction_id
            )
            interaction.status = "failed"
            session.commit()
            return {"status": "error", "detail": "No text content"}

        # ── Steps 5–17: Run shared pipeline ──────────────────────────
        _run_pipeline(session, interaction_id, segments_dicts, tenant, interaction)

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


@celery_app.task(name="orchestrator_daily_all_tenants")
def orchestrator_daily_all_tenants() -> Dict[str, Any]:
    """Daily consolidation of delta reports into profile versions.

    Iterates every tenant and runs :meth:`Orchestrator.run_daily`.  One
    tenant failing does not block the others. Also refreshes each
    tenant's paralinguistic baselines so scorers have up-to-date
    percentiles for the "hot voice" / "flat tone" signals.
    """
    from backend.app.models import Tenant
    from backend.app.services.orchestrator import get_orchestrator

    session = _get_sync_session()
    orch = get_orchestrator()
    totals: Dict[str, int] = {}
    baselines_refreshed = 0
    processed = 0
    try:
        for tenant in session.query(Tenant).all():
            try:
                counts = orch.run_daily(session, tenant.id)
                for k, v in counts.items():
                    totals[k] = totals.get(k, 0) + v
                processed += 1
            except Exception:  # noqa: BLE001 — per-tenant isolation
                logger.exception(
                    "Daily orchestrator failed for tenant %s", tenant.id
                )
            try:
                if _refresh_paralinguistic_baselines(session, tenant):
                    baselines_refreshed += 1
            except Exception:
                logger.exception(
                    "Paralinguistic baseline refresh failed for tenant %s",
                    tenant.id,
                )
    finally:
        session.close()
    return {
        "tenants_processed": processed,
        "profile_updates": totals,
        "paralinguistic_baselines_refreshed": baselines_refreshed,
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
