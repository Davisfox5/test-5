"""Triage service — fast complexity scoring via Claude Haiku."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import anthropic

from backend.app.services.llm_client import get_async_anthropic
from backend.app.services.llm_client import model_for_tier

logger = logging.getLogger(__name__)

HAIKU_MODEL = model_for_tier("haiku")

# Bumped manually whenever ``TRIAGE_SYSTEM_PROMPT`` changes materially.
# Persisted to ``interaction_features.triage_prompt_version``.
TRIAGE_PROMPT_VERSION = "2026-05-25.action-plan-domain-classification"

# Threshold above which a per-call domain prediction overrides the
# team/tenant default. Locked at 0.8: only high-confidence
# reclassifications flip the active template for a single call.
DOMAIN_OVERRIDE_CONFIDENCE_THRESHOLD = 0.8

TRIAGE_SYSTEM_PROMPT = (
    "You are an expert call analyst doing a fast first-pass triage on a "
    "transcript. Your tone is calm, attentive, and honest.\n\n"
    "Given a compressed transcript excerpt and metadata, score the complexity "
    "of the interaction and return ONLY valid JSON with these fields:\n"
    "- complexity_score: float 0.0–1.0 (0 = trivial, 1 = extremely complex)\n"
    "- quick_summary: one-sentence summary of the call in neutral third "
    "person (\"The customer asked about renewal pricing.\", \"Routine "
    "check-in with no open issues.\")\n"
    "- sentiment_overall: one of 'positive', 'neutral', 'negative', 'mixed'\n"
    "- topics: list of short topic labels mentioned in the call\n"
    "- domain_prediction: {domain, confidence} where domain is one of "
    "'sales' | 'customer_service' | 'it_support' | 'generic' and "
    "confidence is a float 0.0-1.0. This classifies which playbook the "
    "downstream Action Plan synthesizer should use. Decision guide:\n"
    "  * 'sales' - prospecting, demo, pricing, contract, renewal expansion, "
    "competitive eval. Customer is evaluating buying or expanding.\n"
    "  * 'customer_service' - post-sale support, refunds, billing disputes, "
    "retention, account changes, complaint resolution.\n"
    "  * 'it_support' - bugs, incidents, repro requests, integration "
    "issues, performance problems, technical how-tos.\n"
    "  * 'generic' - the call doesn't fit any of the above cleanly OR is "
    "ambiguous. Use generic when uncertain rather than guessing.\n"
    "  Set confidence to >= 0.8 only when the domain is unambiguous "
    "(clear churn-and-refund call, clear demo, clear bug report). "
    "Set 0.5-0.7 for partial signals. Set < 0.5 when guessing.\n\n"
    "Factors that increase complexity: multiple topics, escalation language, "
    "compliance/legal references, competitor mentions, churn signals, technical "
    "troubleshooting, emotional distress, multi-party involvement.\n\n"
    "Return ONLY the JSON object, no markdown fences or extra text."
)

# Rough chars-per-token estimate for truncation.
_CHARS_PER_TOKEN = 4
_MAX_TRANSCRIPT_CHARS = 2000 * _CHARS_PER_TOKEN  # ~2000 tokens


def _strip_json_fences(raw: str) -> str:
    """Strip markdown code fences and surrounding whitespace/prose from Claude responses.

    Haiku often ignores instructions to return bare JSON and wraps it in ```json ... ```.
    This helper finds the first { or [ and last } or ] and returns just the JSON payload.
    """
    text = raw.strip()
    # Strip common fence patterns
    if text.startswith("```"):
        # Remove first line (```json or ```)
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        # Remove trailing fence
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Fallback: slice from first { or [ to last } or ]
    if text and text[0] not in "{[":
        start_obj = text.find("{")
        start_arr = text.find("[")
        starts = [s for s in (start_obj, start_arr) if s >= 0]
        if starts:
            text = text[min(starts):]
    if text and text[-1] not in "}]":
        end_obj = text.rfind("}")
        end_arr = text.rfind("]")
        end = max(end_obj, end_arr)
        if end >= 0:
            text = text[: end + 1]
    return text.strip()


class TriageService:
    """Score call complexity with Claude Haiku to decide analysis tier."""

    def __init__(self) -> None:
        self._client = get_async_anthropic()

    async def score_complexity(
        self, transcript_text: str, metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Return a complexity triage dict for the given transcript.

        If ``complexity_score < 0.4`` the call is simple and only needs Haiku
        analysis.  Otherwise it should be escalated to Sonnet.
        """
        compressed = transcript_text[:_MAX_TRANSCRIPT_CHARS]

        user_content = (
            f"## Call Metadata\n"
            f"- Channel: {metadata.get('channel', 'unknown')}\n"
            f"- Duration (seconds): {metadata.get('duration', 'unknown')}\n"
            f"- Caller: {metadata.get('caller_info', 'unknown')}\n\n"
            f"## Transcript Excerpt\n{compressed}"
        )

        try:
            # Triage system prompt is constant across all calls; wrap it as
            # a cacheable system block so we hit Anthropic's prompt cache on
            # every triage after the first within the cache TTL window.
            response = await self._client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": TRIAGE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )

            raw_text = response.content[0].text
            result: Dict[str, Any] = json.loads(_strip_json_fences(raw_text))

            # Ensure required keys exist with sensible defaults.
            result.setdefault("complexity_score", 0.5)
            result.setdefault("quick_summary", "")
            result.setdefault("sentiment_overall", "neutral")
            result.setdefault("topics", [])

            # Normalize the domain prediction. Defaults to generic with
            # confidence 0.0 when the model didn't include it (older
            # prompts) or returned a bad shape — the Action Plan
            # synthesizer falls back to the team/tenant default in that
            # case, exactly as if no prediction had been made.
            dp = result.get("domain_prediction")
            if not isinstance(dp, dict):
                dp = {}
            dp_domain = str(dp.get("domain") or "generic").strip()
            if dp_domain not in {
                "sales", "customer_service", "it_support", "generic",
            }:
                dp_domain = "generic"
            try:
                dp_conf = float(dp.get("confidence") or 0.0)
            except (TypeError, ValueError):
                dp_conf = 0.0
            dp_conf = max(0.0, min(1.0, dp_conf))
            result["domain_prediction"] = {
                "domain": dp_domain,
                "confidence": dp_conf,
            }

            # Determine tier recommendation.
            score = float(result["complexity_score"])
            result["recommended_tier"] = "haiku" if score < 0.4 else "sonnet"

            return result

        except json.JSONDecodeError as exc:
            logger.error("Triage JSON parse error: %s", exc)
            return {
                "complexity_score": 0.5,
                "quick_summary": "Unable to parse triage result",
                "sentiment_overall": "neutral",
                "topics": [],
                "recommended_tier": "sonnet",
                "domain_prediction": {"domain": "generic", "confidence": 0.0},
                "error": str(exc),
            }
        except anthropic.APIError as exc:
            logger.error("Anthropic API error during triage: %s", exc)
            return {
                "complexity_score": 0.5,
                "quick_summary": "Triage failed — defaulting to Sonnet",
                "sentiment_overall": "neutral",
                "topics": [],
                "recommended_tier": "sonnet",
                "domain_prediction": {"domain": "generic", "confidence": 0.0},
                "error": str(exc),
            }
