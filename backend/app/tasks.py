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
        # Dev-only: check pgvector health once a day at 00:30 UTC. Emits a
        # distinctive WARN log on sustained breach and optionally opens a
        # GitHub issue. Zero cost — reuses existing Redis + DB.
        "vector-health-daily": {
            "task": "vector_health_daily",
            "schedule": crontab(minute=30, hour=0),
        },
        # Weekly: read the last 14 days of outcomes per tenant and refine
        # the playbook_insights section of each tenant's brief.
        "tenant-brief-refiner-weekly": {
            "task": "tenant_brief_refiner_weekly",
            "schedule": crontab(minute=45, hour=1, day_of_week=1),
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
    tenant_context = dict(getattr(tenant, "tenant_context", None) or {})

    # Pull the customer's living brief if this interaction is tied to a
    # contact that belongs to a Customer row. Gives Sonnet account-specific
    # grounding on top of the tenant-wide playbook.
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

    insights: Dict[str, Any] = asyncio.run(
        _get_analysis_service().analyze(
            compressed_for_llm,
            tier=recommended_tier,
            triage_result=triage_result,
            tenant_context=tenant_context,
            customer_brief=customer_brief,
        )
    )
    logger.info("AI analysis complete for interaction %s", interaction_id)

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

    # Kick a debounced customer-brief rebuild so LINDA has a fresh dossier
    # for the next call with this customer. Best-effort; if Redis/Celery are
    # unavailable in this env we'll catch up on the next interaction.
    if cust_id_for_rebuild is not None:
        try:
            from backend.app.services.kb.context_dispatch import (
                schedule_customer_brief_rebuild,
            )

            asyncio.run(schedule_customer_brief_rebuild(tenant.id, cust_id_for_rebuild))
        except Exception:
            logger.debug("schedule_customer_brief_rebuild failed", exc_info=True)

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

    # ── Step 17: Fire outbound webhooks (placeholder) ────────────────
    logger.info(
        "TODO: Fire outbound webhooks for interaction %s (tenant %s)",
        interaction_id, tenant_id,
    )

    session.commit()
    logger.info("Pipeline complete for interaction %s", interaction_id)


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
    """Batch pipeline for a text-based interaction (chat, email, SMS, etc.).

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
