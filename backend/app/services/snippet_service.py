"""Snippet service — extract and auto-promote notable call segments."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Snippet types that qualify for automatic library promotion.
_EXEMPLARY_TYPES = {"exemplary", "objection_handled"}


class SnippetService:
    """Identify notable segments from AI analysis and prepare snippet records."""

    def identify_notable_segments(
        self,
        insights: Dict[str, Any],
        agent_id: str,
        tenant_id: str,
    ) -> List[Dict[str, Any]]:
        """Build a list of snippet dicts ready for DB insertion.

        Reads ``insights["notable_snippets"]`` and ``insights["key_moments"]``
        produced by :class:`AIAnalysisService`, merges them, and applies
        auto-promotion rules.

        Parameters
        ----------
        insights:
            Full AI analysis output dict.
        agent_id:
            The agent (rep) who handled the call.
        tenant_id:
            The tenant / organisation that owns this call.

        Returns
        -------
        list[dict]
            Snippet dicts with fields suitable for direct DB insert.
        """
        snippets: List[Dict[str, Any]] = []
        seen_keys: set = set()  # deduplicate by (start_time, end_time, title)

        # --- Gather from notable_snippets ---------------------------------
        for ns in insights.get("notable_snippets", []):
            key = (
                ns.get("start_time", ""),
                ns.get("end_time", ""),
                ns.get("title", ""),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)

            snippet = self._build_snippet(
                source=ns,
                agent_id=agent_id,
                tenant_id=tenant_id,
                snippet_type=ns.get("type", "notable"),
                quality=ns.get("quality", "neutral"),
            )
            snippets.append(snippet)

        # --- Gather from key_moments --------------------------------------
        for km in insights.get("key_moments", []):
            key = (
                km.get("start_time", km.get("time", "")),
                km.get("end_time", ""),
                km.get("description", ""),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)

            snippet = self._build_snippet(
                source={
                    "start_time": km.get("start_time", km.get("time", "")),
                    "end_time": km.get("end_time", ""),
                    "type": km.get("type", "key_moment"),
                    "quality": "neutral",
                    "title": km.get("type", "Key Moment"),
                    "description": km.get("description", ""),
                    "tags": [],
                },
                agent_id=agent_id,
                tenant_id=tenant_id,
                snippet_type=km.get("type", "key_moment"),
                quality="neutral",
            )
            snippets.append(snippet)

        # --- Auto-promotion from coaching compliance_gaps -----------------
        compliance_gaps = (
            insights.get("coaching", {}).get("compliance_gaps", [])
        )
        for idx, gap in enumerate(compliance_gaps):
            gap_key = ("compliance", str(idx), gap)
            if gap_key in seen_keys:
                continue
            seen_keys.add(gap_key)

            snippet: Dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "agent_id": agent_id,
                "tenant_id": tenant_id,
                "start_time": "",
                "end_time": "",
                "snippet_type": "compliance_gap",
                "quality": "negative",
                "title": "Compliance Gap",
                "description": gap,
                "transcript_excerpt": "",
                "tags": ["compliance"],
                "in_library": True,
                "library_category": "flagged",
            }
            snippets.append(snippet)

        # --- Apply auto-promotion rules to all snippets -------------------
        for s in snippets:
            self._apply_auto_promotion(s)

        return snippets

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_snippet(
        source: Dict[str, Any],
        agent_id: str,
        tenant_id: str,
        snippet_type: str,
        quality: str,
    ) -> Dict[str, Any]:
        """Create a normalised snippet dict from a raw source entry."""
        return {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "start_time": source.get("start_time", ""),
            "end_time": source.get("end_time", ""),
            "snippet_type": snippet_type,
            "quality": quality,
            "title": source.get("title", ""),
            "description": source.get("description", ""),
            "transcript_excerpt": source.get("transcript_excerpt", ""),
            "tags": source.get("tags", []),
            "in_library": False,
            "library_category": None,
        }

    @staticmethod
    def _apply_auto_promotion(snippet: Dict[str, Any]) -> None:
        """Mutate *snippet* in-place based on auto-promotion rules.

        Rules
        -----
        * Compliance gaps → ``in_library=True, library_category="flagged"``
        * Positive quality + exemplary/objection_handled type →
          ``in_library=True, library_category="best_practice"``
        * Negative quality → ``in_library=True, library_category="training"``
        """
        stype = snippet.get("snippet_type", "")
        quality = snippet.get("quality", "neutral")

        # Already promoted (e.g. compliance gaps built above).
        if snippet.get("in_library"):
            return

        if stype == "compliance_gap":
            snippet["in_library"] = True
            snippet["library_category"] = "flagged"
        elif quality == "positive" and stype in _EXEMPLARY_TYPES:
            snippet["in_library"] = True
            snippet["library_category"] = "best_practice"
        elif quality == "negative":
            snippet["in_library"] = True
            snippet["library_category"] = "training"
