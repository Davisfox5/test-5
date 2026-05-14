"""AI analysis service — deep transcript analysis via Claude Sonnet / Haiku."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import anthropic  # noqa: F401 — referenced in the ``except anthropic.APIError`` clause below

from backend.app.services import metrics as _metrics
from backend.app.services.kb.context_builder import format_brief_for_prompt
from backend.app.services.kb.customer_brief_builder import (
    format_customer_brief_for_prompt,
)
from backend.app.services.llm_client import compute_max_tokens, get_async_anthropic
from backend.app.services.triage_service import _strip_json_fences

logger = logging.getLogger(__name__)

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}

# Bumped manually whenever ``ANALYSIS_SYSTEM_PROMPT`` changes materially.
# Persisted to ``interaction_features.analysis_prompt_version`` so we can
# cohort outcome data by prompt version when training the Phase 4 classifier.
ANALYSIS_PROMPT_VERSION = "2026-05-07.phase-2-paralinguistic"

ANALYSIS_SYSTEM_PROMPT = (
    "You are an expert call analyst reviewing a sales or customer-service "
    "transcript. Your tone is calm, attentive, and honest — never robotic, "
    "never fawning. Ground every observation in evidence from the "
    "transcript.\n\n"
    "Analyze the provided transcript and return ONLY valid JSON (no markdown "
    "fences) with the following fields:\n\n"
    "- summary: string — a short paragraph summarizing the call in neutral "
    "third person (\"The customer pushed back on pricing.\", \"The rep "
    "reframed the objection around ROI.\")\n"
    "- sentiment_overall: 'positive' | 'neutral' | 'negative' | 'mixed' — "
    "this is a coarse bucket, not a calibrated score; the system maps it to "
    "a numeric value downstream so don't try to second-guess the "
    "calibration.\n"
    "- sentiment_trajectory: list of {time: str, score: float 0-10} tracking "
    "sentiment over the call. The trajectory is the only place a numeric "
    "scale is appropriate (it's a within-call shape, not a calibrated "
    "outcome score).\n"
    "- topics: list of {name: str, relevance: float 0–1, mentions: int}\n"
    "- key_moments: list of {time: str, type: str, description: str, "
    "start_time: str, end_time: str} — descriptions in neutral third person\n"
    "- competitor_mentions: list of {name: str, context: str, "
    "handled_well: bool}\n"
    "- product_feedback: list of {theme: str, quote: str, sentiment: str}\n"
    "- action_items: list of action items the rep should take next. The "
    "shape captures what other tools miss: not just 'follow up' but who, "
    "how, and why. Each item:\n"
    "    {\n"
    "      title: str — short imperative, neutral third person\n"
    "      description: str — one-sentence context\n"
    "      category: str — short canonical-style label (e.g. 'follow_up', "
    "'commitment_made', 'commitment_owed_by_customer', "
    "'compliance_remediation', 'deal_advance', 'escalation', "
    "'discovery_followup'). Use one of these when it fits; emit a new "
    "label only if none of these capture the item.\n"
    "      priority: 'high' | 'medium' | 'low' — high only when there's a "
    "concrete deadline or risk; default medium\n"
    "      due_date: 'YYYY-MM-DD' | null — populate ONLY when the "
    "transcript contains an explicit OR clearly implicit due date "
    "(\"by Friday\", \"before our exec sync next week\"). Never guess.\n"
    "      next_step_type: 'meeting' | 'phone_call' | 'email' | "
    "'document_send' | 'crm_update' | 'internal_loop_in' | 'other'\n"
    "      recommended_channel: 'email' | 'phone_call' | 'meeting' | "
    "'document_send' — the medium that best advances this item. "
    "Decide based on: factual vs nuanced (factual → email; "
    "decision/negotiation → phone), single deliverable (email) vs "
    "multi-party discussion (meeting), customer's verbal preference "
    "in the call (\"shoot me an email\" vs \"let's talk\"), urgency, "
    "rapport state.\n"
    "      channel_reasoning: str — one sentence explaining the channel "
    "choice in plain language, in neutral third person.\n"
    "      participants: list of {name: str, role: str, side: 'customer'"
    "|'vendor', source: 'named_in_call'|'mentioned_in_call'|"
    "'inferred_from_topic'} — every named person who should be on the "
    "next step, including specialists from the rep's own team who should "
    "be looped in based on the topics raised (e.g. 'Sales Engineer' "
    "for technical questions, 'Legal' for contract terms). Empty list "
    "is fine for solo follow-ups.\n"
    "      prep_artifacts: list of str — what the rep should prepare "
    "(deck slides, pricing tier sheet, customer's stated scale numbers, "
    "etc.). Empty list is fine.\n"
    "      email_draft: {subject: str, body: str} | null — populate when "
    "recommended_channel is 'email' or 'document_send'. Body in the "
    "rep's voice, ready to edit and send.\n"
    "      call_script: list of str | null — bullet talking points when "
    "recommended_channel is 'phone_call' or 'meeting'. Each bullet is "
    "one talking point, in the rep's voice.\n"
    "      implicit_signal: str | null — set when there's something the "
    "rep may not have noticed that drove this action item (a customer "
    "hesitation, a deferred question the rep didn't catch, a stakeholder "
    "the customer name-dropped without context). Plain language, "
    "third person. The rep should be able to read this and recognize "
    "what they missed. Null when the action item is obvious.\n"
    "      suggested_attachments: list of {title: str, reason: str} — "
    "suggested supporting documents the rep should attach when sending. "
    "Free-form titles describing what kind of document fits (e.g. "
    "\"API rate limit sheet\", \"Pricing tier overview\", \"Onboarding "
    "checklist\"). The system maps these to actual KB docs at the UI "
    "layer; the rep reviews and confirms before send. Empty list is "
    "fine when no document attachment is warranted.\n"
    "    }\n"
    "- coaching: {what_went_well: list[str], improvements: list[str], "
    "script_adherence_band: 'high' | 'medium' | 'low' | 'failing', "
    "compliance_gaps: list[str]} — phrase what_went_well and improvements "
    "as direct second-person notes to the rep (\"You did a great job "
    "framing…\", \"Next time, try…\"). script_adherence_band is a coarse "
    "bucket (the system converts it to a numeric score downstream).\n"
    "- follow_up_email_draft: {subject: str, body: str} — body written in "
    "the rep's voice, ready for them to edit and send\n"
    "- churn_risk_signal: 'high' | 'medium' | 'low' | 'none' — coarse "
    "bucket only. Do NOT emit a numeric churn_risk; the system maps the "
    "bucket to a calibrated number downstream.\n"
    "- upsell_signal: 'high' | 'medium' | 'low' | 'none' — coarse bucket "
    "only. Same downstream-mapping treatment as churn_risk_signal.\n"
    "- notable_snippets: list of {start_time: str, end_time: str, "
    "type: str, quality: 'positive'|'negative'|'neutral', title: str, "
    "description: str, tags: list[str]} — descriptions in neutral third "
    "person (\"This is where the customer pushed back on pricing.\")\n"
    "- inline_tags: list of {start_time: str, end_time: str, speaker: str, "
    "type: 'went_well' | 'improvement' | 'competitor' | 'commitment' | "
    "'objection_resolved' | 'objection_unresolved' | 'tense', "
    "popup_text: str, suggested_action: str | null} — per-moment tags meant "
    "for inline rendering on the transcript with a hover popup. popup_text "
    "is one short sentence of context; suggested_action is a one-line nudge "
    "or null. Empty list is fine when no taggable moments exist.\n"
    "- customer_signals: {commitment_language: list[str], "
    "change_talk: list[str], sustain_talk: list[str], "
    "trust_signals: list[str], urgency_language: list[str], "
    "objections: list of {quote: str, resolved: bool}} — verbatim "
    "customer-side quotes organized by signal type. Powers downstream "
    "behavior analysis. Empty lists are fine.\n"
    "- methodology_coverage: {framework: str, covered: list[str], "
    "missing: list[str], next_question: str | null} — when the tenant "
    "context specifies a sales or service methodology (SPIN, MEDDIC, "
    "structured-resolution), score which stages were covered and suggest "
    "one question that would address the most-missed stage. Default to "
    "{\"framework\": \"none\", \"covered\": [], \"missing\": [], "
    "\"next_question\": null} when no methodology is specified.\n"
    "- evidence: {objection_count: int, unresolved_objection_count: int, "
    "commitment_count: int, discovery_questions: int, "
    "competitor_mention_count: int} — counts of grounded events in the "
    "transcript. These are MEASUREMENTS (you observed them), not "
    "predictions. Be precise: only count items you can point at in the "
    "transcript. Zeros are honest answers; do not pad. The system uses "
    "these to compute deterministic rubric scores alongside the LLM "
    "buckets.\n\n"
    "Be thorough but concise. Ground every observation in evidence from the "
    "transcript. Never invent quotes. Keep the JSON schema exactly as "
    "specified."
)


def _format_transcript(
    segments: List[Dict[str, Any]],
    inline_tags: Optional[Dict[int, str]] = None,
) -> str:
    """Convert transcript segments to a readable string.

    When ``inline_tags`` is provided, the per-segment-index tag string
    (already pre-formatted by ``paralinguistic_prompt`` as ``"[pitch
    ↑1.8σ · pause-before 1.6σ]"``) gets appended after the time/
    speaker prefix on the matching turn. Absent indices render
    bit-identical to the no-tag path — callers can pass ``None`` to
    short-circuit the lookup entirely.
    """
    lines: List[str] = []
    for idx, seg in enumerate(segments):
        time = seg.get("time", seg.get("start_time", "00:00"))
        speaker = seg.get("speaker", "Unknown")
        text = seg.get("text", "")
        tag = inline_tags.get(idx) if inline_tags else None
        if tag:
            lines.append(f"[{time}] {speaker} {tag}: {text}")
        else:
            lines.append(f"[{time}] {speaker}: {text}")
    return "\n".join(lines)


class AIAnalysisService:
    """Run deep AI analysis on call transcripts."""

    def __init__(self) -> None:
        self._client = get_async_anthropic()

    async def analyze(
        self,
        transcript_segments: List[Dict[str, Any]],
        tier: str = "sonnet",
        triage_result: Optional[Dict[str, Any]] = None,
        system_prompt_override: Optional[str] = None,
        tenant_context_block: Optional[str] = None,
        rag_context_block: Optional[str] = None,
        max_tokens_override: Optional[int] = None,
        tenant_context: Optional[Dict[str, Any]] = None,
        customer_brief: Optional[Dict[str, Any]] = None,
        paralinguistic_block: Optional[Any] = None,
        complexity_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Analyze a transcript and return structured insights.

        Parameters
        ----------
        transcript_segments:
            List of dicts with keys ``time``/``start_time``, ``speaker``, ``text``.
        tier:
            ``"haiku"`` for simple calls, ``"sonnet"`` for complex calls.
        triage_result:
            Optional output from :class:`TriageService` to give the model context.
        system_prompt_override:
            If provided, used in place of ``ANALYSIS_SYSTEM_PROMPT`` (prompt-variant swap).
        tenant_context_block:
            Pre-formatted tenant block appended to the user message. Takes
            precedence over ``tenant_context`` when provided.
        rag_context_block:
            Optional knowledge-base excerpts retrieved for this specific call.
        max_tokens_override:
            Per-tenant parameter override for ``max_tokens``.
        tenant_context:
            Raw tenant brief dict; auto-formatted and injected as a cacheable
            system block when ``tenant_context_block`` is not provided.
        customer_brief:
            Raw customer brief dict; auto-formatted as a system block.
        """
        model = MODELS.get(tier, MODELS["sonnet"])
        # Phase 2: paralinguistic block contributes both a structured
        # prompt section and a per-turn-index inline tag map. When the
        # tenant flag is off, the audio is unavailable, or the
        # extractor returns ``available: False``, ``paralinguistic_block``
        # is None and the formatted transcript is bit-identical to the
        # pre-Phase-2 output. Decision Q3: silent fallback.
        inline_tags = (
            getattr(paralinguistic_block, "inline_tags", None)
            if paralinguistic_block is not None
            else None
        )
        para_structured = (
            getattr(paralinguistic_block, "structured_text", "")
            if paralinguistic_block is not None
            else ""
        )
        formatted = _format_transcript(transcript_segments, inline_tags)
        system_prompt = system_prompt_override or ANALYSIS_SYSTEM_PROMPT

        # Build user message, optionally prepending triage + tenant + RAG context.
        parts: List[str] = []
        if triage_result:
            summary = triage_result.get("quick_summary", "")
            topics = ", ".join(triage_result.get("topics", []))
            parts.append(
                f"## Triage Context\n"
                f"Quick summary: {summary}\n"
                f"Detected topics: {topics}\n"
            )
        if tenant_context_block:
            parts.append(tenant_context_block)
        if rag_context_block:
            parts.append(rag_context_block)
        if para_structured:
            parts.append(para_structured)
        parts.append(f"## Transcript\n{formatted}")
        user_content = "\n".join(parts)

        raw_text = ""

        # Assemble system blocks. Tenant context first (most stable for prompt
        # caching), customer brief second, analyst instructions last. If a
        # system_prompt_override is provided (prompt-variant path), it replaces
        # the analyst instructions block.
        system_blocks: List[Dict[str, Any]] = []
        tenant_text = (
            None
            if tenant_context_block  # already appended to user message above
            else format_brief_for_prompt(tenant_context or {})
        )
        if tenant_text:
            system_blocks.append(
                {
                    "type": "text",
                    "text": tenant_text,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        customer_text = format_customer_brief_for_prompt(customer_brief or {})
        if customer_text:
            system_blocks.append(
                {
                    "type": "text",
                    "text": customer_text,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        system_blocks.append(
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        )

        # Tiered max_tokens: cheaper baseline for simple calls; full ceiling
        # for high-complexity main analysis or long inputs. Explicit overrides
        # are honored but capped to the tier's hard ceiling.
        approx_input_tokens = (
            sum(len(b.get("text", "")) for b in system_blocks) + len(user_content)
        ) // 4
        budget = compute_max_tokens(
            tier,
            input_tokens=approx_input_tokens,
            task_type="main_analysis",
            complexity_score=complexity_score,
            explicit_override=max_tokens_override,
        )

        try:
            t0 = time.perf_counter()
            response = await self._client.messages.create(
                model=model,
                max_tokens=budget,
                system=system_blocks,
                messages=[{"role": "user", "content": user_content}],
            )
            _metrics.LLM_LATENCY.labels(surface="analysis", model=model).observe(
                time.perf_counter() - t0
            )

            raw_text = response.content[0].text
            stop_reason = response.stop_reason
            # Stamp every result with stop_reason + raw response length
            # so we have postmortem visibility without log access. When
            # we see ``_stop_reason='max_tokens'`` AND a low _raw_chars
            # we know the cap is firing; when stop_reason='end_turn'
            # but parse fails we know the issue is malformed JSON not
            # truncation.
            stamp = {
                "_stop_reason": stop_reason,
                "_raw_chars": len(raw_text),
                "_max_tokens_budget": budget,
            }
            if stop_reason == "max_tokens":
                logger.warning(
                    "AI analysis hit max_tokens — output truncated at %d chars (budget=%d)",
                    len(raw_text), budget,
                )
            cleaned = _strip_json_fences(raw_text)
            try:
                result: Dict[str, Any] = json.loads(cleaned)
                result.update(stamp)
                return result
            except json.JSONDecodeError as parse_exc:
                # Best-effort repair: truncated responses (max_tokens cut off
                # the model mid-emit) leave dangling strings / arrays /
                # objects that ``json-repair`` can stitch closed. We accept
                # the partial result rather than leaving every long-form
                # call with empty insights.
                logger.warning(
                    "AI analysis JSON parse failed (%s); attempting repair",
                    parse_exc,
                )
                try:
                    from json_repair import repair_json  # type: ignore
                    repaired = repair_json(cleaned, return_objects=True)
                    if isinstance(repaired, dict) and repaired:
                        repaired.setdefault("_recovered", True)
                        repaired.update(stamp)
                        return repaired
                except Exception as repair_exc:
                    logger.error("json-repair fallback failed: %s", repair_exc)
                # Final fallback — preserve the raw text in summary so the
                # row isn't completely empty.
                fallback = {
                    "summary": raw_text,
                    "error": f"JSON parse error: {parse_exc}",
                }
                fallback.update(stamp)
                return fallback
        except anthropic.APIError as exc:
            logger.error("Anthropic API error during analysis: %s", exc)
            return {"error": f"Anthropic API error: {exc}"}
