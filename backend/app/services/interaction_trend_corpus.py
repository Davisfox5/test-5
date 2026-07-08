"""Shared text-extraction helper for Interaction-based trend callers.

Sales and CS domain interactions share the same ``insights`` JSONB
surface (see ``ai_analysis.py``'s system prompts): ``topics``,
``competitor_mentions``, ``objections``, ``product_feedback``,
``concerns_raised``. Both ``sales_trend_detector.py`` and
``cs_trend_detector.py`` need to fold one interaction's insights into a
single representative string before it goes to the embedder, so the
logic lives here once instead of twice.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def representative_text(insights: Any) -> Optional[str]:
    """Fold one interaction's structured insights into a short string
    for embedding/clustering.

    Returns ``None`` when nothing usable was found (analysis hasn't run
    yet, or the call genuinely surfaced no topics/objections/feedback) —
    the caller skips those rows rather than embedding an empty string.
    """
    if not isinstance(insights, dict):
        return None
    parts: List[str] = []

    def _names(key: str, name_key: str = "name", limit: int = 3) -> None:
        items = insights.get(key)
        if not isinstance(items, list):
            return
        names = [
            i.get(name_key)
            for i in items
            if isinstance(i, dict) and isinstance(i.get(name_key), str) and i.get(name_key)
        ]
        if names:
            parts.append(", ".join(names[:limit]))

    _names("topics")
    _names("competitor_mentions")

    objections = insights.get("objections")
    if isinstance(objections, list):
        quotes = [
            o.get("quote")
            for o in objections
            if isinstance(o, dict) and isinstance(o.get("quote"), str) and o.get("quote")
        ]
        if quotes:
            parts.append(quotes[0][:200])

    product_feedback = insights.get("product_feedback")
    if isinstance(product_feedback, list):
        feedback = [f for f in product_feedback if isinstance(f, str) and f]
        if feedback:
            parts.append(feedback[0][:200])

    concerns = insights.get("concerns_raised")
    if isinstance(concerns, list):
        topics = [
            c.get("topic")
            for c in concerns
            if isinstance(c, dict) and isinstance(c.get("topic"), str) and c.get("topic")
        ]
        if topics:
            parts.append(", ".join(topics[:3]))

    return " | ".join(parts) if parts else None
