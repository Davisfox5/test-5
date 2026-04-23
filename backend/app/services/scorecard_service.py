"""Scorecard service — QA scoring of calls against configurable templates.

Two entrypoints:

* :meth:`ScorecardService.score` — single template, one LLM call.
* :meth:`ScorecardService.score_many` — many templates against the same
  transcript, one **batched** LLM call. Cheaper than calling ``score`` in a
  loop because the transcript + insights block is repeated only once. Falls
  back to per-template calls automatically if the batched response fails
  to parse (parse errors in one criterion won't take out siblings).
"""

from __future__ import annotations

import asyncio
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
    "call transcript, AI-generated insights about the call, and one or more "
    "scorecard templates, each with weighted criteria.\n\n"
    "For every criterion of every template, assign a score from 0 up to the "
    "criterion's max weight and provide brief reasoning grounded in specific "
    "evidence from the transcript.\n\n"
    "Return ONLY valid JSON (no markdown fences) with this structure:\n"
    "{\n"
    '  "templates": [\n'
    "    {\n"
    '      "template_id": "<id>",\n'
    '      "total_score": <float>,\n'
    '      "criterion_scores": [\n'
    "        {\n"
    '          "name": "<criterion name>",\n'
    '          "score": <int 0..max>,\n'
    '          "max": <int>,\n'
    '          "reasoning": "<1-2 sentence justification>"\n'
    "        }\n"
    "      ]\n"
    "    }\n"
    "  ]\n"
    "}\n\n"
    "When only one template is requested you MAY omit the ``templates`` "
    "array and return the single object directly at the top level.\n\n"
    "Each template's total_score must equal the sum of its criterion scores. "
    "Be fair but rigorous — only award full marks when the evidence clearly "
    "supports it."
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

    # ── Batched scoring ────────────────────────────────────────────────

    async def score_many(
        self,
        transcript_segments: List[Dict[str, Any]],
        templates: List[Dict[str, Any]],
        insights: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Score a transcript against ``len(templates)`` templates in one LLM call.

        Returns a list aligned with ``templates`` (same order, same length),
        each entry shaped like :meth:`score`'s return with an extra
        ``template_id`` key copied from the input.

        If the batched call fails to parse, we fall back to per-template
        scoring transparently so one flaky template doesn't take down the
        rest.
        """
        if not templates:
            return []

        # Trivial case: single template — use the existing path, no batching
        # overhead and preserves the original tight schema.
        if len(templates) == 1:
            t = templates[0]
            single = await self.score(transcript_segments, t, insights)
            single["template_id"] = t.get("id") or t.get("template_id")
            return [single]

        formatted = _format_transcript(transcript_segments)
        summary = insights.get("summary", "N/A")
        sentiment = insights.get("sentiment_overall", "N/A")

        template_blocks: List[str] = []
        for t in templates:
            tid = t.get("id") or t.get("template_id") or t.get("name")
            criteria_block = "\n".join(
                f"  - {c['name']} (max {c['weight']} pts): {c.get('description', '')}"
                for c in t.get("criteria", [])
            )
            template_blocks.append(
                f"### Template {tid} — {t.get('name', 'Untitled')}\n"
                f"{criteria_block}"
            )

        user_content = (
            "## Scorecard Templates\n"
            + "\n\n".join(template_blocks)
            + "\n\n"
            f"## Call Insights\n"
            f"Summary: {summary}\n"
            f"Sentiment: {sentiment}\n\n"
            f"## Transcript\n{formatted}"
        )

        try:
            response = await self._client.messages.create(
                model=HAIKU_MODEL,
                # Scales with template count — each template produces a few
                # hundred tokens. Cap so we don't blow past sensible limits.
                max_tokens=min(8192, 1024 + 512 * len(templates)),
                system=SCORECARD_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw_text = response.content[0].text
            parsed = json.loads(_strip_json_fences(raw_text))
        except (json.JSONDecodeError, anthropic.APIError) as exc:
            logger.warning(
                "Batched scoring failed (%s); falling back to per-template calls", exc,
            )
            return await self._score_many_fallback(
                transcript_segments, templates, insights
            )

        by_id: Dict[str, Dict[str, Any]] = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("templates"), list):
            for entry in parsed["templates"]:
                tid = str(entry.get("template_id") or "")
                if tid:
                    by_id[tid] = entry

        results: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        for t in templates:
            tid = str(t.get("id") or t.get("template_id") or t.get("name") or "")
            entry = by_id.get(tid)
            if entry is None:
                # Model dropped this template — score it in a single-shot
                # call rather than losing it silently.
                missing.append(t)
                continue
            criterion_scores = entry.get("criterion_scores") or []
            total = round(
                sum(float(c.get("score", 0)) for c in criterion_scores), 2
            )
            results.append(
                {
                    "template_id": tid,
                    "total_score": total,
                    "criterion_scores": criterion_scores,
                }
            )

        if missing:
            rescued = await self._score_many_fallback(
                transcript_segments, missing, insights
            )
            results.extend(rescued)

        # Preserve the input template order.
        order = {
            str(t.get("id") or t.get("template_id") or t.get("name") or ""): i
            for i, t in enumerate(templates)
        }
        results.sort(key=lambda r: order.get(str(r.get("template_id") or ""), 1_000_000))
        return results

    async def _score_many_fallback(
        self,
        transcript_segments: List[Dict[str, Any]],
        templates: List[Dict[str, Any]],
        insights: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Per-template fallback used when batched scoring parses poorly."""
        async def _one(t: Dict[str, Any]) -> Dict[str, Any]:
            out = await self.score(transcript_segments, t, insights)
            out["template_id"] = (
                t.get("id") or t.get("template_id") or t.get("name")
            )
            return out

        return await asyncio.gather(*[_one(t) for t in templates])
