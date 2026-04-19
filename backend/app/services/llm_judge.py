"""LLM-as-judge: scores AI outputs against rubrics.

Three judge surfaces — analysis, email_classifier, email_reply — each run
asynchronously after the producer.  Output rows live in
``insight_quality_scores`` and feed Layer 7 dashboards + Layer 6 regression
detection.

Design choices:
- **Model:** Claude Haiku (fast, cheap; calibrate vs. Sonnet monthly).
- **Caching:** rubric system prompt uses ``cache_control: ephemeral`` like the
  producers — same cross-tenant cache hit pattern.
- **Skip rules** (per plan):
    - SMS interactions under 3 segments → skip entirely
    - Email replies under 50 chars → skip LLM dimensions; edit-distance is
      computed inline in :func:`backend.app.services.feedback_service`.

Each judge returns ``{"status": ..., "scores_written": int, "composite": float}``.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from typing import Any, Dict, List, Optional

import anthropic
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.models import (
    FeedbackEvent,
    InsightQualityScore,
    Interaction,
    Tenant,
)
from backend.app.services.triage_service import _strip_json_fences

logger = logging.getLogger(__name__)

JUDGE_MODEL = "claude-haiku-4-5-20251001"
EVALUATOR_ID = JUDGE_MODEL


# ── Rubrics (system prompts for each judge) ──────────────────────────────


ANALYSIS_RUBRIC = (
    "You are an expert quality evaluator for an AI conversation-analysis system. "
    "Score the AI's analysis against the source transcript on five dimensions, "
    "each as a float in [0, 1]:\n\n"
    "- summary_faithfulness: Is the summary an accurate, hallucination-free "
    "compression of the transcript?\n"
    "- action_item_extractability: Are the listed action items actually "
    "mentioned in the transcript?  Are obviously stated next steps missing?\n"
    "- action_item_priority: Are urgency markers ('asap', 'before Q3', "
    "'no rush', etc.) mapped to the right high/medium/low buckets?\n"
    "- coaching_specificity: Are coaching points concrete and call-specific, "
    "or generic boilerplate that could apply to any call?\n"
    "- sentiment_calibration: Does the sentiment_score (0-10) and the "
    "churn_risk / upsell_score line up with observable transcript valence?\n\n"
    "Return ONLY a JSON object with this shape (no markdown fences):\n"
    "{\n"
    '  "scores": {\n'
    '    "summary_faithfulness":     {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "action_item_extractability":{"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "action_item_priority":     {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "coaching_specificity":     {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "sentiment_calibration":    {"score": <float>, "reasoning": "<one sentence>"}\n'
    "  }\n"
    "}\n\n"
    "Be critical.  When the AI hallucinates, fabricates, or generalises "
    "instead of grounding in the transcript, the score must reflect that."
)


CLASSIFIER_RUBRIC = (
    "You are evaluating an email classification model's verdict.  Given the "
    "tenant's internal domains, the email metadata, and the model's verdict, "
    "score on three dimensions in [0, 1]:\n\n"
    "- is_external_correctness: Did the model correctly decide internal vs. "
    "external?  An internal email mis-classified as external is a serious "
    "false positive (would leak internal chatter into client-facing analysis).\n"
    "- category_correctness: For external emails, was the bucket "
    "(sales / support / it / other) the right one?\n"
    "- confidence_calibration: Was the confidence score appropriate?  High "
    "confidence on a clear case = good; high confidence on an ambiguous case "
    "= bad calibration.\n\n"
    "Return ONLY a JSON object:\n"
    "{\n"
    '  "scores": {\n'
    '    "is_external_correctness":  {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "category_correctness":     {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "confidence_calibration":   {"score": <float>, "reasoning": "<one sentence>"}\n'
    "  }\n"
    "}"
)


REPLY_RUBRIC = (
    "You are evaluating an AI-drafted email reply.  Given the inbound message "
    "(or thread tail) and the drafted reply (subject + body + cited KB "
    "snippets), score on five LLM dimensions in [0, 1]:\n\n"
    "- coherence: Does the reply respond to what the customer actually asked, "
    "in well-formed English?\n"
    "- factuality: Are factual claims (prices, SLAs, capabilities, deadlines) "
    "grounded in the cited KB snippets — not fabricated?\n"
    "- tone_match: Does the reply match the tenant's stated tone "
    "(see metadata at top)?\n"
    "- kb_groundedness: Do the cited KB snippets actually support the "
    "claims the reply makes?\n"
    "- review_flag_calibration: Was the requires_human_review flag set "
    "appropriately given the actual content?  Pricing / commitments / legal "
    "should flip it on; pure FAQ-style answers should not.\n\n"
    "Return ONLY a JSON object:\n"
    "{\n"
    '  "scores": {\n'
    '    "coherence":               {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "factuality":              {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "tone_match":              {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "kb_groundedness":         {"score": <float>, "reasoning": "<one sentence>"},\n'
    '    "review_flag_calibration": {"score": <float>, "reasoning": "<one sentence>"}\n'
    "  }\n"
    "}"
)


# Composite weights — keep in sync with the plan's tables (Layer 2).
_ANALYSIS_WEIGHTS = {
    "summary_faithfulness": 0.25,
    "action_item_extractability": 0.30,
    "action_item_priority": 0.15,
    "coaching_specificity": 0.15,
    "sentiment_calibration": 0.15,
}
_CLASSIFIER_WEIGHTS = {
    "is_external_correctness": 0.50,
    "category_correctness": 0.30,
    "confidence_calibration": 0.20,
}
_REPLY_WEIGHTS = {
    "coherence": 0.15,
    "factuality": 0.25,
    "tone_match": 0.15,
    "kb_groundedness": 0.15,
    "review_flag_calibration": 0.10,
    "edit_distance_proxy": 0.20,  # filled separately (ground-truth signal)
}


# ── Common Anthropic call helper (sync wrapper around async API) ─────────


def _call_judge(rubric: str, user_content: str) -> Optional[Dict[str, Any]]:
    """Run the judge synchronously.  Returns parsed scores dict or None."""
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=2048,
            system=[{
                "type": "text",
                "text": rubric,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text
        return json.loads(_strip_json_fences(raw))
    except (anthropic.APIError, json.JSONDecodeError, IndexError) as exc:
        logger.exception("Judge call failed: %s", exc)
        return None


def _persist_scores(
    session: Session,
    *,
    tenant_id: Any,
    interaction_id: Optional[Any],
    conversation_id: Optional[Any],
    surface: str,
    weights: Dict[str, float],
    scores_payload: Dict[str, Any],
    prompt_variant_id: Optional[Any],
    extra: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Persist per-dimension scores and return the composite."""
    rows_payload = dict(scores_payload.get("scores") or {})
    if extra:
        rows_payload.update(extra)
    if not rows_payload:
        return {"status": "empty", "scores_written": 0, "composite": None}

    composite_num = 0.0
    composite_denom = 0.0
    written = 0
    flag_low_dimension = False

    for dim, payload in rows_payload.items():
        try:
            raw_score = payload.get("score") if isinstance(payload, dict) else payload
            score = float(raw_score)
            score = max(0.0, min(1.0, score))
        except (TypeError, ValueError):
            continue
        weight = float(weights.get(dim, 0.0))
        composite_num += weight * score
        composite_denom += weight
        if score < 0.4:
            flag_low_dimension = True
        row = InsightQualityScore(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            conversation_id=conversation_id,
            surface=surface,
            evaluator_type="llm_judge",
            evaluator_id=EVALUATOR_ID,
            dimension=dim,
            score=score,
            reasoning=(payload.get("reasoning") if isinstance(payload, dict) else None),
            prompt_variant_id=prompt_variant_id,
        )
        session.add(row)
        written += 1

    composite = round(composite_num / composite_denom, 4) if composite_denom else None
    session.commit()
    return {
        "status": "ok",
        "scores_written": written,
        "composite": composite,
        "flag_low_dimension": flag_low_dimension,
    }


# ── Analysis judge ───────────────────────────────────────────────────────


def evaluate_analysis(session: Session, interaction_id: str) -> Dict[str, Any]:
    interaction = (
        session.query(Interaction)
        .filter(Interaction.id == _uuid.UUID(interaction_id))
        .first()
    )
    if interaction is None:
        return {"status": "not_found", "scores_written": 0, "composite": None}
    if interaction.channel == "sms" and len(interaction.transcript or []) < 3:
        return {"status": "skipped_short_sms", "scores_written": 0, "composite": None}
    if not interaction.insights:
        return {"status": "no_insights", "scores_written": 0, "composite": None}

    transcript_str = "\n".join(
        f"[{seg.get('time', '00:00')}] {seg.get('speaker', '?')}: {seg.get('text', '')}"
        for seg in (interaction.transcript or [])
    )[:24000]

    user_content = (
        f"## Channel\n{interaction.channel}\n\n"
        f"## AI Output (insights JSON)\n"
        f"{json.dumps(interaction.insights, indent=2)[:12000]}\n\n"
        f"## Source Transcript\n{transcript_str}"
    )

    scores = _call_judge(ANALYSIS_RUBRIC, user_content)
    if scores is None:
        return {"status": "judge_error", "scores_written": 0, "composite": None}

    result = _persist_scores(
        session,
        tenant_id=interaction.tenant_id,
        interaction_id=interaction.id,
        conversation_id=interaction.conversation_id,
        surface="analysis",
        weights=_ANALYSIS_WEIGHTS,
        scores_payload=scores,
        prompt_variant_id=interaction.prompt_variant_id,
    )
    _flag_if_needed(session, interaction, result)
    return result


# ── Classifier judge ─────────────────────────────────────────────────────


def evaluate_classification(session: Session, interaction_id: str) -> Dict[str, Any]:
    interaction = (
        session.query(Interaction)
        .filter(Interaction.id == _uuid.UUID(interaction_id))
        .first()
    )
    if interaction is None:
        return {"status": "not_found", "scores_written": 0, "composite": None}
    if interaction.channel != "email":
        return {"status": "not_email", "scores_written": 0, "composite": None}

    tenant = (
        session.query(Tenant).filter(Tenant.id == interaction.tenant_id).first()
    )
    internal_domains = []
    if tenant is not None:
        internal_domains = (tenant.features_enabled or {}).get(
            "email_internal_domains", []
        )

    user_content = (
        f"## Tenant internal domains\n{', '.join(internal_domains) or '(none configured)'}\n\n"
        f"## Email metadata\n"
        f"From: {interaction.from_address}\n"
        f"To: {', '.join(interaction.to_addresses or [])}\n"
        f"Subject: {interaction.subject or '(no subject)'}\n"
        f"Body preview:\n{(interaction.raw_text or '')[:2000]}\n\n"
        f"## Model verdict\n"
        f"is_external (model decided to ingest as external): "
        f"{not interaction.is_internal}\n"
        f"classification: {interaction.classification}\n"
        f"confidence: {interaction.classification_confidence}"
    )

    scores = _call_judge(CLASSIFIER_RUBRIC, user_content)
    if scores is None:
        return {"status": "judge_error", "scores_written": 0, "composite": None}

    result = _persist_scores(
        session,
        tenant_id=interaction.tenant_id,
        interaction_id=interaction.id,
        conversation_id=interaction.conversation_id,
        surface="email_classifier",
        weights=_CLASSIFIER_WEIGHTS,
        scores_payload=scores,
        prompt_variant_id=interaction.prompt_variant_id,
    )
    _flag_if_needed(session, interaction, result)
    return result


# ── Reply judge ──────────────────────────────────────────────────────────


def evaluate_reply(session: Session, interaction_id: str) -> Dict[str, Any]:
    interaction = (
        session.query(Interaction)
        .filter(Interaction.id == _uuid.UUID(interaction_id))
        .first()
    )
    if interaction is None or interaction.direction != "outbound":
        return {"status": "not_outbound", "scores_written": 0, "composite": None}
    body = interaction.raw_text or ""
    if len(body.strip()) < 50:
        return {"status": "too_short", "scores_written": 0, "composite": None}

    # Most recent inbound message in the same thread, for context.
    inbound = None
    if interaction.conversation_id is not None:
        inbound = (
            session.query(Interaction)
            .filter(
                Interaction.conversation_id == interaction.conversation_id,
                Interaction.direction == "inbound",
            )
            .order_by(Interaction.created_at.desc())
            .first()
        )
    inbound_body = (inbound.raw_text if inbound else "")[:4000]

    tenant = session.query(Tenant).filter(Tenant.id == interaction.tenant_id).first()
    tone = "professional, concise, warm"
    if tenant:
        branding = tenant.branding_config or {}
        tone = branding.get("email_tone") or branding.get("tone") or tone

    user_content = (
        f"## Tenant tone\n{tone}\n\n"
        f"## Inbound message\n{inbound_body}\n\n"
        f"## Drafted reply\nSubject: {interaction.subject or ''}\n\n{body[:8000]}"
    )

    scores = _call_judge(REPLY_RUBRIC, user_content)
    if scores is None:
        return {"status": "judge_error", "scores_written": 0, "composite": None}

    # Pull the edit-distance signal from feedback_events (set synchronously
    # by the conversations send-reply endpoint).
    edit_score = _edit_distance_dimension(session, interaction.id)
    extra = {"edit_distance_proxy": edit_score} if edit_score is not None else None

    result = _persist_scores(
        session,
        tenant_id=interaction.tenant_id,
        interaction_id=interaction.id,
        conversation_id=interaction.conversation_id,
        surface="email_reply",
        weights=_REPLY_WEIGHTS,
        scores_payload=scores,
        prompt_variant_id=interaction.prompt_variant_id,
        extra=extra,
    )
    _flag_if_needed(session, interaction, result)
    return result


def _edit_distance_dimension(
    session: Session, interaction_id: Any
) -> Optional[Dict[str, Any]]:
    """Fetch the edit-distance event for this interaction and convert to a [0,1] score."""
    ev = (
        session.query(FeedbackEvent)
        .filter(
            FeedbackEvent.interaction_id == interaction_id,
            FeedbackEvent.event_type.in_(
                ("reply_sent_unchanged", "reply_edited_before_send")
            ),
        )
        .order_by(FeedbackEvent.created_at.desc())
        .first()
    )
    if ev is None:
        return None
    payload = ev.payload or {}
    sim = float(payload.get("similarity", 1.0))
    return {
        "score": max(0.0, min(1.0, sim)),
        "reasoning": (
            f"Sent body matched draft at similarity {sim:.4f} "
            f"(event_type={ev.event_type})"
        ),
    }


# ── Flag low-quality outputs for review ──────────────────────────────────


def _flag_if_needed(
    session: Session, interaction: Interaction, result: Dict[str, Any]
) -> None:
    """If composite < 0.5 OR any dimension < 0.4, flag for human review."""
    composite = result.get("composite")
    flag_low = result.get("flag_low_dimension")
    if (composite is not None and composite < 0.5) or flag_low:
        try:
            interaction.status = "flagged_for_review"
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to mark interaction flagged_for_review")
        # Best-effort webhook fan-out so enterprise tenants can react.
        try:
            from backend.app.services.webhook_dispatcher import dispatch_sync

            dispatch_sync(
                session,
                interaction.tenant_id,
                "quality.alert",
                {
                    "event": "quality.alert",
                    "tenant_id": str(interaction.tenant_id),
                    "interaction_id": str(interaction.id),
                    "composite": composite,
                    "flag_low_dimension": bool(flag_low),
                },
            )
        except Exception:
            logger.exception("quality.alert webhook dispatch failed (non-fatal)")
