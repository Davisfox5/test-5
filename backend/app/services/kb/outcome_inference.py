"""Outcome inference from AI-analysis insights.

When the post-call analysis runs (``ai_analysis.AIAnalysisService.analyze``),
its JSON output already carries most of what we need to guess the call's
disposition:

* ``coaching.script_adherence_score``
* ``sentiment_overall`` / ``sentiment_score``
* ``churn_risk_signal`` / ``churn_risk``
* ``upsell_signal`` / ``upsell_score``
* ``action_items[].title`` (often mentions "demo scheduled", "refund issued")

This module squeezes those into a single ``outcome_type`` label plus
``outcome_value`` / ``outcome_confidence`` so we can write them back to the
Interaction row and, where relevant, emit a ``CustomerOutcomeEvent``.

Intentionally deterministic (no extra LLM call). The AI guessed the signals;
we just fold them into a normalised shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class InferredOutcome:
    outcome_type: str
    outcome_value: Optional[float]
    outcome_confidence: float
    outcome_notes: Optional[str]
    customer_events: List[Dict[str, Any]]


_OUTCOME_KEYWORDS = {
    "closed_won": ["closed won", "signed the contract", "deal closed", "signed up"],
    "closed_lost": ["closed lost", "decided not to", "went with competitor", "will not move forward"],
    "demo_scheduled": ["demo scheduled", "booked a demo", "scheduled demo"],
    "booked_meeting": ["booked meeting", "scheduled a call", "follow-up scheduled", "set up a meeting"],
    "proposal_sent": ["proposal sent", "quote sent", "sent pricing"],
    "refund_processed": ["refund issued", "refund processed", "refund approved"],
    "resolved": ["resolved", "fixed the issue", "problem solved", "issue closed"],
    "escalated": ["escalated", "passed to manager", "tier 2", "transferred"],
    "unresolved": ["unresolved", "could not help", "outside our scope"],
    "follow_up_scheduled": ["follow up", "circle back", "check in next"],
    "qualified": ["qualified lead", "passes BANT", "good fit"],
    "disqualified": ["not a fit", "outside our ICP", "wrong segment"],
}


def _scan_keywords(text: str) -> Optional[str]:
    text = (text or "").lower()
    for outcome, needles in _OUTCOME_KEYWORDS.items():
        for n in needles:
            if n in text:
                return outcome
    return None


def infer_outcome(insights: Dict[str, Any]) -> InferredOutcome:
    """Map the AI analysis JSON to a normalised outcome record."""
    customer_events: List[Dict[str, Any]] = []

    summary = (insights or {}).get("summary", "")
    action_items = (insights or {}).get("action_items", []) or []
    action_blob = " ".join(str(a.get("title", "")) for a in action_items if isinstance(a, dict))

    # 1. Look for explicit dispositions in the summary or action items first.
    explicit = _scan_keywords(summary) or _scan_keywords(action_blob)

    churn_signal = (insights or {}).get("churn_risk_signal", "none")
    churn_score = float((insights or {}).get("churn_risk") or 0.0)
    upsell_signal = (insights or {}).get("upsell_signal", "none")
    upsell_score = float((insights or {}).get("upsell_score") or 0.0)
    sentiment_overall = (insights or {}).get("sentiment_overall", "neutral")
    sentiment_score = float((insights or {}).get("sentiment_score") or 0.0)

    # 2. Sales signals override support-style dispositions when strong.
    if explicit in (None, "resolved", "info_shared") and upsell_signal == "high":
        explicit = "upsell_opportunity"
    if churn_signal == "high":
        customer_events.append(
            {
                "event_type": "at_risk_flagged",
                "magnitude": None,
                "signal_strength": churn_score,
                "reason": "AI detected high churn risk",
                "source": "ai_inferred",
            }
        )
    if upsell_signal == "high":
        customer_events.append(
            {
                "event_type": "advocate_signal",
                "magnitude": None,
                "signal_strength": upsell_score,
                "reason": "AI detected strong upsell signal",
                "source": "ai_inferred",
            }
        )
    if explicit == "closed_won":
        customer_events.append(
            {
                "event_type": "became_customer",
                "magnitude": None,
                "signal_strength": 0.9,
                "reason": "Close-won detected by AI summary",
                "source": "ai_inferred",
            }
        )
    if explicit == "closed_lost":
        customer_events.append(
            {
                "event_type": "churned",
                "magnitude": None,
                "signal_strength": 0.8,
                "reason": "Close-lost detected by AI summary",
                "source": "ai_inferred",
            }
        )

    outcome_type = explicit or _fallback_from_sentiment(sentiment_overall, sentiment_score)
    confidence = _confidence_for(explicit, churn_signal, upsell_signal)

    return InferredOutcome(
        outcome_type=outcome_type,
        outcome_value=None,
        outcome_confidence=confidence,
        outcome_notes=None,
        customer_events=customer_events,
    )


def _fallback_from_sentiment(sentiment_overall: str, sentiment_score: float) -> str:
    """When we can't find a clear disposition, fall back to a generic label
    based on sentiment so downstream analytics still have something."""
    if sentiment_overall == "positive" and sentiment_score >= 7:
        return "info_shared"
    if sentiment_overall == "negative" or sentiment_score < 4:
        return "unresolved"
    return "no_decision"


def _confidence_for(
    explicit: Optional[str],
    churn_signal: str,
    upsell_signal: str,
) -> float:
    if explicit:
        return 0.75  # keyword scan hit
    if churn_signal == "high" or upsell_signal == "high":
        return 0.55
    return 0.35
