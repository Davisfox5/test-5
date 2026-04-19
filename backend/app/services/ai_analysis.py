"""AI analysis service — deep transcript analysis via Claude Sonnet / Haiku."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import anthropic

from backend.app.config import get_settings
from backend.app.services.kb.context_builder import format_brief_for_prompt
from backend.app.services.triage_service import _strip_json_fences

logger = logging.getLogger(__name__)

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}

ANALYSIS_SYSTEM_PROMPT = (
    "You are an expert call analyst for a conversation intelligence platform. "
    "Analyze the provided transcript and return ONLY valid JSON (no markdown "
    "fences) with the following fields:\n\n"
    "- summary: string — concise paragraph summarizing the call\n"
    "- sentiment_overall: 'positive' | 'neutral' | 'negative' | 'mixed'\n"
    "- sentiment_score: float 0–10 (10 = most positive)\n"
    "- sentiment_trajectory: list of {time: str, score: float} tracking "
    "sentiment over the call\n"
    "- topics: list of {name: str, relevance: float 0–1, mentions: int}\n"
    "- key_moments: list of {time: str, type: str, description: str, "
    "start_time: str, end_time: str}\n"
    "- competitor_mentions: list of {name: str, context: str, "
    "handled_well: bool}\n"
    "- product_feedback: list of {theme: str, quote: str, sentiment: str}\n"
    "- action_items: list of {title: str, category: str, priority: "
    "'high'|'medium'|'low', due_date: str|null, "
    "suggested_email_draft: str|null}\n"
    "- coaching: {what_went_well: list[str], improvements: list[str], "
    "script_adherence_score: float 0–100, compliance_gaps: list[str]}\n"
    "- follow_up_email_draft: {subject: str, body: str}\n"
    "- churn_risk_signal: 'high' | 'medium' | 'low' | 'none'\n"
    "- churn_risk: float 0.0–1.0 (numeric counterpart: high≈0.85, "
    "medium≈0.55, low≈0.25, none≈0.05 — tune within bucket from evidence)\n"
    "- upsell_signal: 'high' | 'medium' | 'low' | 'none'\n"
    "- upsell_score: float 0.0–1.0 (numeric counterpart with the same "
    "bucket convention as churn_risk)\n"
    "- notable_snippets: list of {start_time: str, end_time: str, "
    "type: str, quality: 'positive'|'negative'|'neutral', title: str, "
    "description: str, tags: list[str]}\n\n"
    "Be thorough but concise. Ground every observation in evidence from the "
    "transcript."
)


def _format_transcript(segments: List[Dict[str, Any]]) -> str:
    """Convert transcript segments to a readable string."""
    lines: List[str] = []
    for seg in segments:
        time = seg.get("time", seg.get("start_time", "00:00"))
        speaker = seg.get("speaker", "Unknown")
        text = seg.get("text", "")
        lines.append(f"[{time}] {speaker}: {text}")
    return "\n".join(lines)


class AIAnalysisService:
    """Run deep AI analysis on call transcripts."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def analyze(
        self,
        transcript_segments: List[Dict[str, Any]],
        tier: str = "sonnet",
        triage_result: Optional[Dict[str, Any]] = None,
        company_context: Optional[Dict[str, Any]] = None,
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
        company_context:
            Optional LINDA-built brief assembled from the tenant's KB. When
            present, injected as a second cacheable system block so the model
            grounds observations in the tenant's own product/policy reality.
        """
        model = MODELS.get(tier, MODELS["sonnet"])
        formatted = _format_transcript(transcript_segments)

        # Build user message, optionally prepending triage context.
        parts: List[str] = []
        if triage_result:
            summary = triage_result.get("quick_summary", "")
            topics = ", ".join(triage_result.get("topics", []))
            parts.append(
                f"## Triage Context\n"
                f"Quick summary: {summary}\n"
                f"Detected topics: {topics}\n"
            )
        parts.append(f"## Transcript\n{formatted}")
        user_content = "\n".join(parts)

        # Assemble the system prompt. The company-context block goes BEFORE
        # the base instructions and gets its own cache_control so repeated
        # calls within a tenant hit the prompt cache.
        system_blocks: List[Dict[str, Any]] = []
        context_text = format_brief_for_prompt(company_context or {})
        if context_text:
            system_blocks.append(
                {
                    "type": "text",
                    "text": context_text,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        system_blocks.append(
            {
                "type": "text",
                "text": ANALYSIS_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        )

        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=8192,
                system=system_blocks,
                messages=[{"role": "user", "content": user_content}],
            )

            raw_text = response.content[0].text
            # Surface truncation as a retry signal (caller marks row as failed).
            if response.stop_reason == "max_tokens":
                logger.warning(
                    "AI analysis hit max_tokens — output truncated at %d chars",
                    len(raw_text),
                )
            result: Dict[str, Any] = json.loads(_strip_json_fences(raw_text))
            return result

        except json.JSONDecodeError as exc:
            logger.error("AI analysis JSON parse error: %s — raw: %s", exc, raw_text)
            # Return whatever we can salvage.
            return {
                "summary": raw_text if "raw_text" in dir() else "",
                "error": f"JSON parse error: {exc}",
            }
        except anthropic.APIError as exc:
            logger.error("Anthropic API error during analysis: %s", exc)
            return {"error": f"Anthropic API error: {exc}"}
