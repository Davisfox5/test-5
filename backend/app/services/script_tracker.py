"""Script adherence tracker — fuzzy keyword matching against call transcripts."""

from __future__ import annotations

import logging
import re
from typing import Dict, List

logger = logging.getLogger(__name__)


class ScriptTrackerService:
    """Checks transcript buffer against a compliance checklist using keyword matching."""

    def __init__(self, checklist_items: List[str]) -> None:
        self.checklist_items = checklist_items
        # Pre-compile lowered keywords for each checklist item.
        # Split each item into constituent words for fuzzy matching.
        self._compiled: List[Dict] = []
        for item in checklist_items:
            words = [w.lower() for w in re.findall(r"\w+", item) if len(w) > 2]
            self._compiled.append({
                "original": item,
                "keywords": words,
                "threshold": max(1, int(len(words) * 0.6)),  # 60% of keywords must match
            })

    def check(self, transcript_buffer: List[dict]) -> dict:
        """Check transcript buffer against checklist items.

        Args:
            transcript_buffer: list of dicts with at least a ``text`` key.

        Returns:
            dict with ``covered``, ``missing``, and ``coverage_pct``.
        """
        # Combine all transcript text into a single lowered blob for matching.
        full_text = " ".join(
            seg.get("text", "") for seg in transcript_buffer
        ).lower()

        covered: List[str] = []
        missing: List[str] = []

        for entry in self._compiled:
            matched_count = sum(
                1 for kw in entry["keywords"] if kw in full_text
            )
            if matched_count >= entry["threshold"]:
                covered.append(entry["original"])
            else:
                missing.append(entry["original"])

        total = len(self.checklist_items)
        coverage_pct = round((len(covered) / total) * 100, 1) if total > 0 else 100.0

        return {
            "covered": covered,
            "missing": missing,
            "coverage_pct": coverage_pct,
        }
