"""Paralinguistic block + inline-tag formatter for the analysis prompt.

Phase 2: takes the raw extractor output (per-speaker aggregates) plus
the ``NotableTag`` list from ``paralinguistic_baseline`` and produces
the prompt block + a per-turn-index map of inline tag strings.

Wording lives here, not in ``ai_analysis.py``, so prompt-only edits
don't touch the orchestration logic. Tests pin the exact rendered
strings so a stealth tweak to the prompt language gets caught at PR
time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from backend.app.services.paralinguistic_baseline import NotableTag

# Header text the analysis prompt looks for. Worth pinning — the AI
# analysis service is allowed to skip the block entirely when None is
# passed, but if it IS rendered the heading needs to stay stable so
# the model develops a consistent attention pattern across calls.
PROMPT_HEADER: str = "## Paralinguistic Features"

# Reading instruction the model gets right after the data. Keep
# imperative and short — the per-feature lines do the heavy lifting.
PROMPT_INSTRUCTION: str = (
    "These are deterministic acoustic measurements (Praat / parselmouth). "
    "Use them to ground sentiment, sarcasm, hesitation, and "
    "commitment-confidence reads. Do NOT invent acoustic features that "
    "aren't listed; absent values mean we couldn't measure them, not "
    "that they're zero."
)


@dataclass
class ParalinguisticPromptBlock:
    """What ``AIAnalysisService.analyze()`` consumes.

    ``structured_text`` is the human-readable block that gets prepended
    between the RAG context and the transcript (or empty string when
    the extractor produced nothing useful).

    ``inline_tags`` is a per-segment-index map of bracketed tag
    strings ready to be pasted into the formatted-transcript line for
    that segment. Empty when there are no notable utterances.
    """

    structured_text: str = ""
    inline_tags: Dict[int, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.structured_text and not self.inline_tags


# Field labels in the per-speaker line. Keys match the actual
# ``ParalinguisticExtractor._measure_slices`` output (paralinguistics.py).
# Owned here so the test pins the canonical render order + unit suffixes.
SPEAKER_FIELDS: Sequence[Dict[str, str]] = (
    {"key": "pitch_hz_p50", "label": "pitch median", "unit": "Hz", "fmt": "{:.0f}"},
    {"key": "pitch_std_semitones", "label": "pitch range", "unit": "semitones", "fmt": "{:.1f}"},
    {"key": "speaking_rate_syll_per_sec", "label": "speaking rate", "unit": "syll/s", "fmt": "{:.2f}"},
    {"key": "pause_rate_per_min", "label": "pause rate", "unit": "/min", "fmt": "{:.1f}"},
    {"key": "intensity_db_p50", "label": "intensity", "unit": "dB", "fmt": "{:.0f}"},
)


# Map ``NotableTag`` feature names → short prompt symbols. Keep the
# arrows directional so the model can read them at a glance.
def _format_z(name: str, z: float) -> str:
    arrow = "↑" if z > 0 else "↓"
    short = {
        "pitch": "pitch",
        "intensity": "loud",
        "speaking_rate": "rate",
        "pause_before": "pause-before",
    }.get(name, name)
    # Magnitude with one decimal sigma. Always positive after we strip
    # sign — the arrow already encodes direction.
    return f"{short} {arrow}{abs(z):.1f}σ"


def build_prompt_block(
    para: Optional[Mapping[str, Any]],
    notable: Sequence[NotableTag],
) -> ParalinguisticPromptBlock:
    """Build the prompt block + inline-tag map.

    ``para`` is the dict produced by
    ``ParalinguisticFeatures.as_dict()`` (with optional arousal /
    emotion annotations). When it's None or ``available`` is False, we
    return an empty block — the orchestration layer skips injection
    entirely (decision Q3: silent fallback).

    ``notable`` is the list returned by
    ``paralinguistic_baseline.notable_utterances``. Each tag becomes
    one entry in ``inline_tags`` keyed on ``segment_idx``.
    """
    if not para or not para.get("available"):
        return ParalinguisticPromptBlock()
    structured = _render_structured(para)
    inline = _render_inline_tags(notable)
    return ParalinguisticPromptBlock(structured_text=structured, inline_tags=inline)


def _render_structured(para: Mapping[str, Any]) -> str:
    """Per-speaker bullet list + reading instruction."""
    per_speaker = para.get("per_speaker") or {}
    if not per_speaker:
        return ""
    lines: List[str] = [PROMPT_HEADER]
    # Speakers ordered alphabetically for deterministic prompt output —
    # otherwise dict-ordering can shift between python runs and the
    # model sees a churning prompt across re-analyses.
    for speaker in sorted(per_speaker.keys()):
        block = per_speaker[speaker] or {}
        if not isinstance(block, Mapping):
            continue
        formatted_fields: List[str] = []
        for spec in SPEAKER_FIELDS:
            val = block.get(spec["key"])
            if val is None:
                continue
            try:
                rendered = spec["fmt"].format(float(val))
            except (TypeError, ValueError):
                continue
            unit = f" {spec['unit']}" if spec["unit"] else ""
            formatted_fields.append(f"{spec['label']} {rendered}{unit}")
        if not formatted_fields:
            continue
        lines.append(f"- {speaker}: " + ", ".join(formatted_fields))

    arousal = para.get("arousal") or {}
    if isinstance(arousal, Mapping) and arousal:
        # Arousal is a single overall number per speaker; render it on
        # its own line so the model treats it as a separate signal,
        # not just another acoustic stat.
        arousal_lines: List[str] = []
        for speaker in sorted(arousal.keys()):
            val = arousal[speaker]
            if val is None:
                continue
            try:
                arousal_lines.append(f"{speaker} arousal {float(val):.2f}")
            except (TypeError, ValueError):
                continue
        if arousal_lines:
            lines.append("- Arousal: " + "; ".join(arousal_lines))

    if len(lines) == 1:
        # Header only, no actual data — return empty so the orchestration
        # skips the block entirely.
        return ""
    lines.append("")
    lines.append(PROMPT_INSTRUCTION)
    return "\n".join(lines)


def _render_inline_tags(notable: Sequence[NotableTag]) -> Dict[int, str]:
    """Build ``{segment_idx: "[pitch ↑1.8σ · pause-before 1.6σ]"}``."""
    out: Dict[int, str] = {}
    for tag in notable:
        if not tag.features:
            continue
        rendered = " · ".join(_format_z(name, z) for name, z in tag.features)
        out[tag.segment_idx] = f"[{rendered}]"
    return out