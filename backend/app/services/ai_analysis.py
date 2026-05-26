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

ANALYSIS_SYSTEM_PROMPT_TERSE = (
    "You are a sales coach reviewing a call. Your voice is clipboard "
    "notes: clean, specific, evidence-cited. Imagine a head coach "
    "writing on a notepad after watching the play. Get in, make the "
    "point, get out.\n\n"
    "VOICE RULES\n"
    "1. Lead with the observation, then the evidence. Never preamble.\n"
    "2. Cite specific moments or short quotes. Use timestamps when "
    "useful.\n"
    "3. One short sentence per item. Hard caps below. Respect them.\n"
    "4. NEVER use em-dashes (—) or en-dashes (–) anywhere in "
    "your output. Zero. Not in summary, not in coaching, not in "
    "snippets, not in JSON string values, not anywhere. Use periods, "
    "colons, commas, semicolons, or parentheses instead. This is "
    "non-negotiable. If your draft contains an em-dash or en-dash, "
    "rewrite it before emitting.\n"
    "5. Banned phrases and characters: em-dash (—), en-dash "
    "(–), 'You did a great job', 'It's important to', "
    "'Remember to', 'Going forward, consider', 'This is a common', "
    "'In conclusion', 'Overall', 'It's worth noting', 'Make sure to'. "
    "If you find yourself reaching for these, you're being too "
    "explanatory.\n"
    "6. Neutral third person in narrative fields (summary, "
    "key_moments, notable_snippets). Coaching is direct second person "
    "but still terse and specific.\n"
    "7. Never invent quotes. If you don't have evidence, leave the "
    "field empty.\n\n"
    "LENGTH BUDGETS (hard caps)\n"
    "- summary: ≤ 60 words, 1-3 sentences\n"
    "- key_moments[].description: ≤ 20 words each\n"
    "- notable_snippets[].description: ≤ 20 words each\n"
    "- coaching.what_went_well[] item: ≤ 25 words each, max 4 items\n"
    "- coaching.improvements[] item: ≤ 25 words each, max 4 items\n"
    "- action_items[].title: ≤ 12 words each\n"
    "- action_items[].description: ≤ 25 words each\n"
    "- action_items[].channel_reasoning: ≤ 20 words\n"
    "- action_items[].implicit_signal: ≤ 25 words\n"
    "- inline_tags[].popup_text: ≤ 20 words each\n\n"
    "STYLE EXAMPLES (mirror these). Note: zero em-dashes anywhere.\n"
    "BAD (too verbose) Coaching item:\n"
    "  'You built excellent rapport from the very first exchange by "
    "affirming the customer's existing policy discipline and "
    "connecting over shared interests like the Navigator vs. Escalade. "
    "This made a lengthy underwriting call feel conversational rather "
    "than clinical, which is great because it kept the customer "
    "engaged throughout.'\n"
    "GOOD (clipboard) Same point, terse:\n"
    "  'Strong rapport opener. \"I take my hat off to you\" for the "
    "existing policy kept underwriting conversational.'\n\n"
    "BAD (too verbose) Improvement note:\n"
    "  'When the customer disclosed 16 medications and multiple daily "
    "dosing windows, you proceeded without explicitly noting this "
    "could affect underwriting tier or rate. Going forward, you should "
    "consider setting a brief expectation so the customer isn't "
    "surprised if a follow-up is needed.'\n"
    "GOOD (clipboard) Same point, terse:\n"
    "  '16-med disclosure at 07:00. Flag underwriting risk to the "
    "customer next time so a follow-up isn't a surprise.'\n\n"
    "BAD (too verbose) Summary:\n"
    "  'A warm-transfer inbound call in which a returning customer "
    "sought to add a second final expense whole-life policy to "
    "complement an existing paid-up policy. The rep conducted full "
    "health and lifestyle underwriting, presented three coverage/"
    "premium options, and the customer selected the mid-tier option. "
    "The rep completed a voice-signature e-application...'\n"
    "GOOD (clipboard) Same point, terse:\n"
    "  'Returning customer added a 2nd final-expense policy. Rep ran "
    "underwriting, presented 3 options, closed mid-tier, set up "
    "auto-draft. Customer disclosed 16 daily meds; flagged for "
    "underwriting review.'\n\n"
    "OUTPUT\n"
    "Return ONLY valid JSON (no markdown fences) with the schema "
    "below. No em-dashes or en-dashes anywhere in any string value.\n\n"
    + (
        "- summary: string. See length budget above.\n"
        "- sentiment_overall: 'positive' | 'neutral' | 'negative' | 'mixed'\n"
        "- sentiment_trajectory: list of {time: str, score: float 0-10}\n"
        "- topics: list of {name: str, relevance: float 0 to 1, mentions: int}\n"
        "- key_moments: list of {time: str, type: str, description: str, "
        "start_time: str, end_time: str}\n"
        "- competitor_mentions: list of {name: str, context: str, "
        "handled_well: bool}\n"
        "- product_feedback: list of {theme: str, quote: str, sentiment: str}\n"
        "- action_items: list of items. See budgets. Each:\n"
        "    {title, description, category (e.g. 'follow_up', "
        "'commitment_made', 'commitment_owed_by_customer', "
        "'compliance_remediation', 'deal_advance', 'escalation', "
        "'discovery_followup'), priority ('high'|'medium'|'low'), "
        "due_date ('YYYY-MM-DD' or null; never guess), "
        "next_step_type ('meeting'|'phone_call'|'email'|"
        "'document_send'|'crm_update'|'internal_loop_in'|'other'), "
        "recommended_channel ('email'|'phone_call'|'meeting'|"
        "'document_send'), channel_reasoning (≤20 words), "
        "participants (list of {name, role, side, source}), "
        "prep_artifacts (list of str), email_draft (or null), "
        "call_script (list of str or null), implicit_signal "
        "(≤25 words or null), suggested_attachments (list of "
        "{title, reason})}\n"
        "- coaching: {what_went_well: list[str], improvements: list[str], "
        "script_adherence_band: 'high'|'medium'|'low'|'failing', "
        "compliance_gaps: list[str]}. Direct 2nd person but terse and "
        "evidence-cited. See budgets and examples above.\n"
        "- follow_up_email_draft: {subject: str, body: str}\n"
        "- churn_risk_signal: 'high'|'medium'|'low'|'none'\n"
        "- upsell_signal: 'high'|'medium'|'low'|'none'\n"
        "- notable_snippets: list of {start_time, end_time, type, "
        "quality ('positive'|'negative'|'neutral'), title, "
        "description (≤20 words), tags: list[str]}\n"
        "- inline_tags: list of {start_time, end_time, speaker, type "
        "('went_well'|'improvement'|'competitor'|'commitment'|"
        "'objection_resolved'|'objection_unresolved'|'tense'), "
        "popup_text (≤20 words), suggested_action (≤20 words or null)}\n"
        "- customer_signals: {commitment_language, change_talk, "
        "sustain_talk, trust_signals, urgency_language, objections}. "
        "Verbatim quotes only; empty lists are fine.\n"
        "- methodology_coverage: {framework, covered, missing, "
        "next_question}. Default to {framework:'none', covered:[], "
        "missing:[], next_question:null} when no methodology applies.\n"
        "- evidence: {objection_count, unresolved_objection_count, "
        "commitment_count, discovery_questions, "
        "competitor_mention_count}. Exact counts of grounded events.\n\n"
        "Keep the JSON schema exactly as specified. Ground every "
        "observation in evidence from the transcript. Never invent "
        "quotes. No em-dashes or en-dashes in any string value."
    )
)


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


def _strip_dashes(obj: Any) -> None:
    """Recursively replace em-dashes / en-dashes in string values with '. '.

    The terse prompt instructs the model to never emit em-dashes, but a few
    still leak through, especially when the model echoes verbatim customer
    quotes that contained one in the source transcript. This is the
    belt-and-suspenders pass: walk the parsed insights dict in place and
    canonicalize the offending glyphs out of every string. The replacement
    is '. ' (period + space) so sentence breaks read naturally; a leftover
    double-space gets collapsed.
    """
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                obj[k] = _scrub_str(v)
            else:
                _strip_dashes(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i] = _scrub_str(v)
            else:
                _strip_dashes(v)


def _scrub_str(s: str) -> str:
    if "—" not in s and "–" not in s:
        return s
    out = s.replace(" — ", ". ").replace("—", ". ").replace(" – ", ". ").replace("–", ". ")
    # Collapse accidental double spaces created by the substitution.
    while "  " in out:
        out = out.replace("  ", " ")
    return out.strip()


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

        # Assemble system blocks. Order matters for prompt caching: Anthropic
        # caches request prefixes, so the MOST stable content goes first to
        # maximize hit rate across calls.
        #
        # Order (most-stable to least-stable):
        #   1. Analyst instructions (system_prompt). Identical for every
        #      analysis call platform-wide — highest hit rate.
        #   2. Tenant context. Stable within a tenant, varies across tenants.
        #   3. Customer brief. Varies per customer (lowest hit rate).
        #
        # Three cache breakpoints (well under Anthropic's max of 4). The user
        # message (transcript) is not cached because it's unique per call.
        system_blocks: List[Dict[str, Any]] = []
        system_blocks.append(
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        )
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

            # Retry-on-truncation: when stop_reason='max_tokens' and the
            # caller didn't explicitly cap us, retry once with double
            # the budget. The terse prompt should keep most calls under
            # 8K; this is the safety net for the long-tail mega-calls
            # (90+ min enterprise stuff) that still overflow. We pay the
            # cost of the second call only on the rare truncation case;
            # the first call's output tokens we paid for either way.
            retried = False
            if (
                stop_reason == "max_tokens"
                and max_tokens_override is None
                and budget < 16384
            ):
                retry_budget = min(budget * 2, 16384)
                logger.warning(
                    "AI analysis truncated at %d tokens; retrying once with budget=%d",
                    budget, retry_budget,
                )
                t1 = time.perf_counter()
                response = await self._client.messages.create(
                    model=model,
                    max_tokens=retry_budget,
                    system=system_blocks,
                    messages=[{"role": "user", "content": user_content}],
                )
                _metrics.LLM_LATENCY.labels(
                    surface="analysis_retry", model=model
                ).observe(time.perf_counter() - t1)
                raw_text = response.content[0].text
                stop_reason = response.stop_reason
                budget = retry_budget
                retried = True
                if stop_reason == "max_tokens":
                    logger.warning(
                        "AI analysis STILL truncated after retry at %d chars (budget=%d)",
                        len(raw_text), budget,
                    )

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
                "_retried": retried,
            }
            if stop_reason == "max_tokens":
                logger.warning(
                    "AI analysis hit max_tokens — output truncated at %d chars (budget=%d)",
                    len(raw_text), budget,
                )
            cleaned = _strip_json_fences(raw_text)
            try:
                result: Dict[str, Any] = json.loads(cleaned)
                _strip_dashes(result)
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
                        _strip_dashes(repaired)
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
