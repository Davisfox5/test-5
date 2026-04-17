"""Scorecard service — QA scoring of calls against configurable templates."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import anthropic

from backend.app.config import get_settings
from backend.app.services.triage_service import _strip_json_fences

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

SCORECARD_SYSTEM_PROMPT = (
    "You are an objective QA evaluator for a call center. You will be given a "
    "call transcript, AI-generated insights about the call, and a scorecard "
    "template with weighted criteria.\n\n"
    "For each criterion, assign a score from 0 up to the criterion's max weight "
    "and provide brief reasoning grounded in specific evidence from the "
    "transcript.\n\n"
    "Return ONLY valid JSON (no markdown fences) with this structure:\n"
    "{\n"
    '  "total_score": <float>,\n'
    '  "criterion_scores": [\n'
    "    {\n"
    '      "name": "<criterion name>",\n'
    '      "score": <int 0..max>,\n'
    '      "max": <int>,\n'
    '      "reasoning": "<1-2 sentence justification>"\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "The total_score must equal the sum of all individual scores. Be fair but "
    "rigorous — only award full marks when the evidence clearly supports it."
)


def _format_transcript(segments: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for seg in segments:
        time = seg.get("time", seg.get("start_time", "00:00"))
        speaker = seg.get("speaker", "Unknown")
        text = seg.get("text", "")
        lines.append(f"[{time}] {speaker}: {text}")
    return "\n".join(lines)


class ScorecardService:
    """Score a call against a QA template using Claude Haiku."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def score(
        self,
        transcript_segments: List[Dict[str, Any]],
        template: Dict[str, Any],
        insights: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Score a call transcript against the given scorecard template.

        Parameters
        ----------
        transcript_segments:
            Transcript segments (list of dicts).
        template:
            Scorecard template, e.g.
            ``{"name": "Sales QA", "criteria": [{"name": "Greeting", "weight": 10, "description": "..."}]}``.
        insights:
            AI analysis output to give the evaluator additional context.
        """
        formatted = _format_transcript(transcript_segments)

        criteria_block = "\n".join(
            f"- {c['name']} (max {c['weight']} pts): {c.get('description', '')}"
            for c in template.get("criteria", [])
        )

        summary = insights.get("summary", "N/A")
        sentiment = insights.get("sentiment_overall", "N/A")

        user_content = (
            f"## Scorecard Template: {template.get('name', 'Untitled')}\n"
            f"{criteria_block}\n\n"
            f"## Call Insights\n"
            f"Summary: {summary}\n"
            f"Sentiment: {sentiment}\n\n"
            f"## Transcript\n{formatted}"
        )

        try:
            response = await self._client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=2048,
                system=SCORECARD_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )

            raw_text = response.content[0].text
            result: Dict[str, Any] = json.loads(_strip_json_fences(raw_text))

            # Validate / recompute total_score for consistency.
            criterion_scores = result.get("criterion_scores", [])
            computed_total = sum(
                float(c.get("score", 0)) for c in criterion_scores
            )
            result["total_score"] = round(computed_total, 2)

            return result

        except json.JSONDecodeError as exc:
            logger.error("Scorecard JSON parse error: %s", exc)
            return {
                "total_score": 0,
                "criterion_scores": [],
                "error": f"JSON parse error: {exc}",
            }
        except anthropic.APIError as exc:
            logger.error("Anthropic API error during scoring: %s", exc)
            return {
                "total_score": 0,
                "criterion_scores": [],
                "error": f"Anthropic API error: {exc}",
            }
