"""Celery worker and task definitions — batch processing pipeline.

All async service calls are wrapped with ``asyncio.run()`` because Celery
tasks execute synchronously.  Database access uses a synchronous
SQLAlchemy session created via :func:`_get_sync_session`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
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
    "callsight",
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
        # Poll every connected email integration every 2 minutes —
        # safety net behind Gmail Pub/Sub / Graph webhooks.
        "email-ingest-poll": {
            "task": "email_ingest_poll",
            "schedule": 120.0,
        },
        # Re-register Gmail watches + Graph subscriptions every 12h.
        # Gmail watches expire in ~7 days; Graph message subs in ~3 days.
        "email-push-renew": {
            "task": "email_push_renew_subscriptions",
            "schedule": 43200.0,
        },
        # ── Continuous AI improvement ─────────────────────────────────
        # Drain the feedback Redis stream into feedback_events every 30s.
        "consume-feedback-stream": {
            "task": "consume_feedback_stream",
            "schedule": 30.0,
        },
        # Refresh per-tenant few-shot pools nightly at 03:00 UTC.
        "refresh-few-shot-pools": {
            "task": "refresh_few_shot_pools",
            "schedule": crontab(minute=0, hour=3),
        },
        # WER aggregation Sundays 02:00 UTC.
        "compute-wer-weekly": {
            "task": "compute_wer_weekly",
            "schedule": crontab(minute=0, hour=2, day_of_week=0),
        },
        # Vocabulary candidate discovery weekly, Sundays 03:00 UTC.
        "discover-vocabulary-candidates": {
            "task": "discover_vocabulary_candidates",
            "schedule": crontab(minute=0, hour=3, day_of_week=0),
        },
        # Vocabulary digest email/Slack weekly, Mondays 09:00 UTC.
        "vocabulary-digest-weekly": {
            "task": "vocabulary_digest_weekly",
            "schedule": crontab(minute=0, hour=9, day_of_week=1),
        },
        # Cross-tenant aggregate metrics — Mondays 00:30 UTC, after
        # tenant_insights_weekly has finished.
        "cross-tenant-aggregate-metrics": {
            "task": "cross_tenant_aggregate_metrics",
            "schedule": crontab(minute=30, hour=0, day_of_week=1),
        },
        # Quality regression watchdog runs hourly; the task itself bails
        # quickly when no rollouts are active.
        "quality-regression-check": {
            "task": "quality_regression_check",
            "schedule": 3600.0,
        },
        # Biweekly variant winner selection (Tue/Fri 04:00 UTC).
        "variant-winner-selection": {
            "task": "variant_winner_selection",
            "schedule": crontab(minute=0, hour=4, day_of_week="2,5"),
        },
        # Campaign variant winner selection (same cadence as variant).
        "campaign-variant-winner-selection": {
            "task": "campaign_variant_winner_selection",
            "schedule": crontab(minute=15, hour=4, day_of_week="2,5"),
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
) -> None:
    """Shared pipeline logic for both voice and text interactions.

    Runs steps 5–16 of the batch pipeline (everything after transcription).
    """
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
    triage_result: Dict[str, Any] = asyncio.run(
        _get_triage_service().score_complexity(compressed_text, metadata)
    )
    complexity_score = float(triage_result.get("complexity_score", 0.5))
    recommended_tier = triage_result.get("recommended_tier", "sonnet")
    logger.info(
        "Triage complete for interaction %s: score=%.2f tier=%s",
        interaction_id, complexity_score, recommended_tier,
    )

    # ── Step 9: AI analysis ──────────────────────────────────────────
    # Resolve the active prompt variant for this tenant (A/B-routed by
    # hash(tenant_id, surface)).  Falls back to the producer's hardcoded
    # constant when no variants are seeded yet.
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

    insights: Dict[str, Any] = asyncio.run(
        _get_analysis_service().analyze(
            compressed_for_llm,
            tier=overrides.get("force_tier") or recommended_tier,
            triage_result=triage_result,
            system_prompt_override=variant.prompt_template,
            tenant_context_block=tenant_block,
            rag_context_block=rag_block,
            max_tokens_override=overrides.get("max_tokens"),
        )
    )
    interaction.prompt_variant_id = _variant_to_uuid(variant.variant_id)
    logger.info(
        "AI analysis complete for interaction %s (variant=%s status=%s)",
        interaction_id, variant.name, variant.status,
    )

    # ── Step 10: Scorecard scoring ───────────────────────────────────
    scorecard_results: List[Dict[str, Any]] = []
    templates = (
        session.query(ScorecardTemplate)
        .filter(ScorecardTemplate.tenant_id == tenant.id)
        .all()
    )
    transcript_for_scoring = _segments_for_llm(segments_dicts)
    for template in templates:
        # Check channel filter — skip if template doesn't apply.
        channel_filter = template.channel_filter
        if channel_filter and interaction.channel not in channel_filter:
            continue
        template_dict = {
            "name": template.name,
            "criteria": template.criteria,
        }
        score_result = asyncio.run(
            _get_scorecard_service().score(
                transcript_for_scoring, template_dict, insights
            )
        )
        score_result["template_id"] = str(template.id)
        scorecard_results.append(score_result)
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
        asyncio.run(
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

    # ── Step 17: Fire outbound webhooks ──────────────────────────────
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

        # ── Steps 3–4: Transcription (placeholder) ───────────────────
        # In the real flow:
        #   1. The audio file path comes from interaction.audio_s3_key or
        #      a temp path passed when the task was enqueued.
        #   2. We download the audio to a local temp file.
        #   3. We call TranscriptionService.transcribe(audio_path, engine,
        #      keyterms=tenant.keyterm_boost_list) to get Segment objects.
        #   4. The Segment objects are converted to dicts for the pipeline.
        #
        # For now, since we need an actual audio file to transcribe, we
        # check if the interaction already has transcript data populated
        # (e.g. from a streaming/real-time path) and use that.  If not,
        # we log a warning and set status to 'transcription_pending'.

        segments_dicts: Optional[List[Dict[str, Any]]] = None

        if interaction.transcript and len(interaction.transcript) > 0:
            # Transcript was pre-populated (e.g. from real-time streaming).
            segments_dicts = interaction.transcript
            logger.info(
                "Using pre-populated transcript (%d segments) for interaction %s",
                len(segments_dicts), interaction_id,
            )
        else:
            # TODO: Implement actual audio download + transcription flow:
            #   audio_path = download_audio(interaction.audio_s3_key)
            #   transcription_svc = TranscriptionService()
            #   segments = asyncio.run(transcription_svc.transcribe(
            #       audio_path,
            #       engine=tenant.transcription_engine,
            #       keyterms=tenant.keyterm_boost_list,
            #   ))
            #   segments_dicts = _segments_to_dicts(segments)
            logger.warning(
                "No audio transcription available for interaction %s. "
                "Transcript is empty and audio download is not yet implemented.",
                interaction_id,
            )
            interaction.status = "transcription_pending"
            session.commit()
            return {
                "status": "transcription_pending",
                "detail": "Audio transcription not yet implemented",
            }

        # ── Steps 5–17: Run shared pipeline ──────────────────────────
        _run_pipeline(session, interaction_id, segments_dicts, tenant, interaction)

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
