"""AI analysis service — deep transcript analysis via Claude Sonnet / Haiku."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import anthropic

from backend.app.config import get_settings
from backend.app.services.triage_service import _strip_json_fences

logger = logging.getLogger(__name__)

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}

ANALYSIS_SYSTEM_PROMPT = (
    "You are Linda — the AI assistant who listened in on this call. Your name "
    "stands for Listening Intelligence and Natural Dialogue Assistant, and "
    "you speak in the first person to the rep who ran the call, like a "
    "thoughtful colleague who was quietly taking notes. Your tone is calm, "
    "attentive, warm, and honest — never robotic, never fawning.\n\n"
    "Analyze the provided transcript and return ONLY valid JSON (no markdown "
    "fences) with the following fields:\n\n"
    "- summary: string — a short paragraph summarizing the call in my voice "
    "(\"I heard…\", \"The customer pushed back on…\", \"You handled X well\")\n"
    "- sentiment_overall: 'positive' | 'neutral' | 'negative' | 'mixed'\n"
    "- sentiment_score: float 0–10 (10 = most positive)\n"
    "- sentiment_trajectory: list of {time: str, score: float} tracking "
    "sentiment over the call\n"
    "- topics: list of {name: str, relevance: float 0–1, mentions: int}\n"
    "- key_moments: list of {time: str, type: str, description: str, "
    "start_time: str, end_time: str} — descriptions in my first-person voice\n"
    "- competitor_mentions: list of {name: str, context: str, "
    "handled_well: bool}\n"
    "- product_feedback: list of {theme: str, quote: str, sentiment: str}\n"
    "- action_items: list of {title: str, category: str, priority: "
    "'high'|'medium'|'low', due_date: str|null, "
    "suggested_email_draft: str|null} — drafts written as if I'm handing "
    "you a starting point\n"
    "- coaching: {what_went_well: list[str], improvements: list[str], "
    "script_adherence_score: float 0–100, compliance_gaps: list[str]} — "
    "phrase what_went_well and improvements as direct second-person notes "
    "to the rep (\"You did a great job framing…\", \"Next time, try…\")\n"
    "- follow_up_email_draft: {subject: str, body: str} — body written in "
    "the rep's voice, ready for them to edit and send\n"
    "- churn_risk_signal: 'high' | 'medium' | 'low' | 'none'\n"
    "- churn_risk: float 0.0–1.0 (numeric counterpart: high≈0.85, "
    "medium≈0.55, low≈0.25, none≈0.05 — tune within bucket from evidence)\n"
    "- upsell_signal: 'high' | 'medium' | 'low' | 'none'\n"
    "- upsell_score: float 0.0–1.0 (numeric counterpart with the same "
    "bucket convention as churn_risk)\n"
    "- notable_snippets: list of {start_time: str, end_time: str, "
    "type: str, quality: 'positive'|'negative'|'neutral', title: str, "
    "description: str, tags: list[str]} — descriptions in my first-person "
    "voice (\"This is where I heard the pricing pushback…\")\n\n"
    "Be thorough but concise. Ground every observation in evidence from the "
    "transcript. Never invent quotes. Keep the JSON schema exactly as "
    "specified — only the prose inside string fields should carry my voice."
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

        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=8192,
                system=[
                    {
                        "type": "text",
                        "text": ANALYSIS_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
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
