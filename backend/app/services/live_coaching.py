"""Live coaching service — incremental real-time hints via Claude Haiku."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import anthropic

from backend.app.config import get_settings
from backend.app.services.kb.context_builder import format_brief_for_prompt

logger = logging.getLogger(__name__)

COACHING_MODEL = "claude-haiku-4-5-20251001"

COACHING_SYSTEM_PROMPT = (
    "You are a real-time call coaching assistant for a conversation intelligence "
    "platform. Based on the conversation state and new dialogue, provide 1-3 short, "
    "actionable coaching hints that help the agent handle the call better.\n\n"
    "Guidelines:\n"
    "- Keep hints under 20 words each.\n"
    "- Focus on what the agent should do NOW, not what they should have done.\n"
    "- Track sentiment changes and flag compliance gaps.\n"
    "- If knowledge-base hits are provided, reference them when relevant.\n"
    "- Update the conversation state summary to be compact (~100 words max).\n\n"
    "Respond in JSON only (no markdown fences):\n"
    "{\n"
    '  "hints": [{"hint": "...", "confidence": 0.0-1.0}],\n'
    '  "updated_state": {\n'
    '    "previous_summary": "...",\n'
    '    "sentiment_trend": "improving|stable|declining",\n'
    '    "topics_so_far": ["..."],\n'
    '    "compliance_status": {"disclosed_recording": true/false, ...}\n'
    "  }\n"
    "}"
)


class LiveCoachingService:
    """Provides incremental coaching hints during a live call."""

    def __init__(self) -> None:
        settings = get_settings()
        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def hint_incremental(
        self,
        new_segments: List[dict],
        previous_state: dict,
        kb_hits: Optional[List[dict]] = None,
        company_context: Optional[dict] = None,
    ) -> dict:
        """Generate coaching hints from new transcript segments and prior state.

        Args:
            new_segments: Recent transcript segments (each has ``text``, ``speaker``).
            previous_state: Compact JSON state from the last coaching round.
            kb_hits: Optional top-K RAG results from the knowledge base.
            company_context: Optional LINDA-built brief assembled from the
                tenant's KB. Injected into the system prompt so live coaching
                is grounded in the tenant's own product/policy reality.

        Returns:
            Dict with ``hints`` and ``updated_state``.
        """
        # Build the user message with state + new segments + optional KB hits.
        transcript_block = "\n".join(
            f"[{seg.get('speaker', '?')}]: {seg.get('text', '')}"
            for seg in new_segments
        )

        user_parts = [
            "## Previous conversation state",
            json.dumps(previous_state, default=str),
            "",
            "## New transcript segments",
            transcript_block,
        ]

        if kb_hits:
            kb_block = "\n".join(
                f"- {hit.get('title', 'Untitled')}: {hit.get('snippet', '')}"
                for hit in kb_hits[:3]
            )
            user_parts.extend(["", "## Relevant knowledge-base documents", kb_block])

        user_message = "\n".join(user_parts)

        system_prompt = COACHING_SYSTEM_PROMPT
        context_text = format_brief_for_prompt(company_context or {})
        if context_text:
            system_prompt = f"{context_text}\n\n---\n\n{COACHING_SYSTEM_PROMPT}"

        try:
            response = await self.client.messages.create(
                model=COACHING_MODEL,
                max_tokens=300,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )

            raw_text = response.content[0].text
            result = json.loads(raw_text)

            # Validate expected keys exist.
            if "hints" not in result or "updated_state" not in result:
                logger.warning("Coaching response missing expected keys: %s", raw_text)
                return self._fallback_response(previous_state)

            return result

        except json.JSONDecodeError:
            logger.exception("Failed to parse coaching response as JSON")
            return self._fallback_response(previous_state)
        except anthropic.APIError:
            logger.exception("Anthropic API error during live coaching")
            return self._fallback_response(previous_state)

    @staticmethod
    def _fallback_response(previous_state: dict) -> dict:
        """Return a safe fallback when coaching fails."""
        return {
            "hints": [],
            "updated_state": previous_state if previous_state else {
                "previous_summary": "",
                "sentiment_trend": "stable",
                "topics_so_far": [],
                "compliance_status": {},
            },
        }
