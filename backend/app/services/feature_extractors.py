"""Deterministic conversation features — zero-LLM-cost metrics.

Wraps :class:`CallMetricsService` for the basics and adds the metrics
identified as high-leverage in ``docs/SCORING_ARCHITECTURE.md`` but
missing from the original pipeline:

- Patience (median pre-speech pause after customer stops)
- Interactivity (speaker switches per minute)
- Turn-taking entropy (Shannon entropy of turn distribution)
- Longest customer story (mirror of longest_monologue for the customer)
- Back-channel rate (per speaker)
- Pause distribution (p50 / p90 / max of between-turn gaps)
- Linguistic Style Matching (LSM) — Pennebaker function-word similarity
- Pronoun ratios (we/i, inclusive speech marker)
- Laughter events
- Stakeholder count (distinct speakers)

All computations are deterministic from diarized transcript timestamps
and text.  Results compose into the ``deterministic`` JSONB written to
``InteractionFeatures``.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from statistics import median
from typing import Any, Dict, List, Optional, Sequence

from backend.app.services.call_metrics import CallMetricsService
from backend.app.services.transcription import Segment


# ── Lexicons ──────────────────────────────────────────────────────────────

# A compact LIWC-style function-word set grouped into 9 categories.
# Pennebaker's LSM is traditionally computed across these 9 buckets; the
# dictionary below is intentionally small (public, non-proprietary) —
# tenants can extend it via configuration.
_LSM_CATEGORIES: Dict[str, set] = {
    "pronouns_personal": {
        "i", "me", "my", "mine", "myself",
        "you", "your", "yours", "yourself", "yourselves",
        "he", "him", "his", "she", "her", "hers",
        "we", "us", "our", "ours", "ourselves",
        "they", "them", "their", "theirs", "themselves",
        "it", "its", "itself",
    },
    "pronouns_impersonal": {
        "this", "that", "these", "those", "there", "here",
        "something", "anything", "everything", "nothing",
        "someone", "anyone", "everyone", "noone",
    },
    "articles": {"a", "an", "the"},
    "prepositions": {
        "of", "in", "on", "at", "by", "for", "with", "to", "from",
        "as", "about", "into", "over", "under", "between", "through",
        "against", "during", "without", "within", "along", "across",
    },
    "auxiliary_verbs": {
        "am", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "having",
        "do", "does", "did", "doing",
        "can", "could", "shall", "should", "will", "would",
        "may", "might", "must",
    },
    "high_freq_adverbs": {
        "very", "really", "just", "so", "too", "also", "now", "then",
        "here", "there", "well", "even", "only", "still", "never", "always",
        "sometimes", "often", "again", "already",
    },
    "conjunctions": {
        "and", "but", "or", "nor", "yet", "so", "because", "although",
        "though", "while", "whereas", "since", "if", "unless", "until",
        "when", "whenever", "where", "wherever",
    },
    "negations": {"no", "not", "never", "none", "nothing", "nobody", "nowhere"},
    "quantifiers": {
        "all", "some", "any", "each", "every", "few", "many", "much",
        "several", "both", "more", "less", "most", "least",
    },
}

# Back-channels: short listener tokens that signal engagement without
# claiming a turn.  Matched as standalone utterances (<= 5 words and
# dominated by these tokens).
_BACK_CHANNEL_TOKENS: set = {
    "mm", "mhm", "mm-hmm", "mmhmm", "uh-huh", "uhhuh",
    "yeah", "yep", "yup", "right", "ok", "okay", "sure",
    "gotcha", "got it", "exactly", "absolutely", "totally",
    "understood", "i see", "makes sense",
}

# Laughter tokens that most ASRs emit for laugh events.  Word-boundary
# anchors are only applied to the alphabetic variants (haha+, hehe+, lol);
# bracketed / parenthesized markers have non-word characters at both ends
# which would break ``\b`` matching at start-of-string / end-of-string.
_LAUGHTER_RE = re.compile(
    r"(?:\b(?:haha+|hehe+|lol)\b|\[laughter\]|\(laughter\)|\(laughs\))",
    re.IGNORECASE,
)

# Word tokenizer — simple, Unicode-aware.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


# ── Helpers ───────────────────────────────────────────────────────────────


def _tokens(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _category_counts(tokens: Sequence[str]) -> Dict[str, int]:
    counts = {cat: 0 for cat in _LSM_CATEGORIES}
    for tok in tokens:
        for cat, vocab in _LSM_CATEGORIES.items():
            if tok in vocab:
                counts[cat] += 1
    return counts


def _agent_ids(
    segments: Sequence[Segment],
    explicit: Optional[List[str]],
) -> set:
    """Resolve which speaker_ids belong to the agent.

    Matches the heuristic in :class:`CallMetricsService` so metrics align.
    """
    if explicit:
        return set(explicit)
    first = next((s.speaker_id for s in segments if s.speaker_id is not None), None)
    return {first} if first is not None else set()


def _looks_like_backchannel(text: str) -> bool:
    """True when an utterance is a short listener acknowledgement."""
    if not text:
        return False
    lowered = text.strip().lower()
    # Strip punctuation for matching but preserve spaces.
    normalized = re.sub(r"[^\w\s-]", "", lowered)
    words = normalized.split()
    if not words or len(words) > 5:
        return False
    # Require that a majority of tokens are back-channel tokens / stop
    # words so single filler-heavy utterances don't get counted.
    joined = " ".join(words)
    if joined in _BACK_CHANNEL_TOKENS:
        return True
    hits = sum(1 for w in words if w in _BACK_CHANNEL_TOKENS)
    return hits / max(len(words), 1) >= 0.5


# ── Main computation ──────────────────────────────────────────────────────


class FeatureExtractor:
    """Compute every deterministic feature for an interaction in one pass."""

    def __init__(self) -> None:
        self._base = CallMetricsService()

    def extract(
        self,
        segments: List[Segment],
        agent_speaker_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return the full ``deterministic`` feature dict.

        Safe to call on an empty transcript — returns zeroed fields so the
        feature store always has a consistent shape.
        """
        base = self._base.compute(segments, agent_speaker_ids=agent_speaker_ids)
        if not segments:
            return self._empty(base)

        agents = _agent_ids(segments, agent_speaker_ids)
        total_duration = max(segments[-1].end - segments[0].start, 0.001)
        total_minutes = total_duration / 60.0

        # ── Gaps between consecutive speaker turns ──────────────────────
        # Walk merged per-speaker runs so ASR-level multi-segment turns
        # by the same speaker are treated as one turn.
        turns = self._merge_same_speaker_runs(segments)
        gaps: List[float] = []
        patience_gaps: List[float] = []  # gaps where customer → agent
        switches = 0
        for i in range(1, len(turns)):
            prev, curr = turns[i - 1], turns[i]
            gap = max(curr["start"] - prev["end"], 0.0)
            gaps.append(gap)
            if prev["speaker_id"] != curr["speaker_id"]:
                switches += 1
                # Patience = the pause the agent leaves after the customer
                # stops talking (gap preceding an agent turn that follows
                # a non-agent turn).
                if prev["speaker_id"] not in agents and curr["speaker_id"] in agents:
                    patience_gaps.append(gap)

        interactivity_per_min = round(switches / total_minutes, 3) if total_minutes else 0.0
        patience_sec = round(median(patience_gaps), 3) if patience_gaps else 0.0

        pause_distribution = self._pause_distribution(gaps)

        # ── Turn-taking entropy over speakers ───────────────────────────
        turn_counts = Counter(t["speaker_id"] for t in turns if t["speaker_id"])
        entropy = self._normalized_entropy(turn_counts)

        # ── Longest monologue per role ──────────────────────────────────
        longest_by_speaker = self._longest_by_speaker(turns)
        agent_longest = max(
            (d for sid, d in longest_by_speaker.items() if sid in agents),
            default=0.0,
        )
        customer_longest = max(
            (d for sid, d in longest_by_speaker.items() if sid not in agents),
            default=0.0,
        )

        # ── Back-channel rate per role ──────────────────────────────────
        bc_agent = bc_customer = 0
        agent_minutes = customer_minutes = 0.0
        laughter = 0
        for seg in segments:
            duration = max(seg.end - seg.start, 0.0)
            sid = seg.speaker_id or "__unknown__"
            if sid in agents:
                agent_minutes += duration / 60.0
                if _looks_like_backchannel(seg.text):
                    bc_agent += 1
            else:
                customer_minutes += duration / 60.0
                if _looks_like_backchannel(seg.text):
                    bc_customer += 1
            if seg.text and _LAUGHTER_RE.search(seg.text):
                laughter += 1

        back_channel_rate_per_min = {
            "agent": round(bc_agent / agent_minutes, 2) if agent_minutes else 0.0,
            "customer": round(bc_customer / customer_minutes, 2) if customer_minutes else 0.0,
        }

        # ── Linguistic Style Matching (LSM) and pronoun ratios ──────────
        agent_tokens: List[str] = []
        customer_tokens: List[str] = []
        for seg in segments:
            sid = seg.speaker_id or "__unknown__"
            (agent_tokens if sid in agents else customer_tokens).extend(_tokens(seg.text))

        lsm = self._lsm(agent_tokens, customer_tokens)
        pronoun_ratio_we_i = self._pronoun_ratio(agent_tokens, customer_tokens)

        # ── Assemble the deterministic blob ─────────────────────────────
        out: Dict[str, Any] = {
            # Inherit from CallMetricsService — don't re-derive.
            "talk_pct": {
                "agent": round(base["talk_pct"] / 100, 4),
                "customer": round(base["listen_pct"] / 100, 4),
                "silence": round(base["silence_pct"] / 100, 4),
            },
            "filler_rate_per_min": {
                "agent": (
                    round(base["filler_word_count"] / agent_minutes, 2)
                    if agent_minutes else 0.0
                ),
            },
            "question_rate_per_min": (
                round(base["question_count"] / total_minutes, 2) if total_minutes else 0.0
            ),
            "words_per_min": base["speech_rate_wpm"],
            "interruption_count_total": base["interruptions"],
            # New deterministic features.
            "longest_monologue_sec": {
                "agent": round(agent_longest, 2),
                "customer": round(customer_longest, 2),
            },
            "longest_customer_story_sec": round(customer_longest, 2),
            "interactivity_per_min": interactivity_per_min,
            "patience_sec": patience_sec,
            "turn_entropy": entropy,
            "back_channel_rate_per_min": back_channel_rate_per_min,
            "pause_distribution_sec": pause_distribution,
            "laughter_events": laughter,
            "linguistic_style_match": lsm,
            "pronoun_ratio_we_i": pronoun_ratio_we_i,
            "stakeholder_count": len({t["speaker_id"] for t in turns if t["speaker_id"]}),
            "total_duration_sec": round(total_duration, 2),
            "total_turns": len(turns),
        }
        return out

    # ── Sub-computations ──────────────────────────────────────────────────

    @staticmethod
    def _merge_same_speaker_runs(segments: Sequence[Segment]) -> List[Dict[str, Any]]:
        """Collapse consecutive segments by the same speaker into one turn."""
        turns: List[Dict[str, Any]] = []
        for seg in segments:
            sid = seg.speaker_id
            if turns and turns[-1]["speaker_id"] == sid:
                turns[-1]["end"] = seg.end
                turns[-1]["text"] = (turns[-1]["text"] + " " + (seg.text or "")).strip()
            else:
                turns.append({
                    "speaker_id": sid,
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text or "",
                })
        return turns

    @staticmethod
    def _pause_distribution(gaps: Sequence[float]) -> Dict[str, float]:
        """Return p50 / p90 / max of the between-turn gap distribution."""
        if not gaps:
            return {"p50": 0.0, "p90": 0.0, "max": 0.0}
        sorted_g = sorted(gaps)
        n = len(sorted_g)

        def pct(p: float) -> float:
            # Linear interpolation between nearest ranks.
            idx = p * (n - 1)
            lo, hi = int(idx), min(int(idx) + 1, n - 1)
            frac = idx - lo
            return sorted_g[lo] + (sorted_g[hi] - sorted_g[lo]) * frac

        return {
            "p50": round(pct(0.5), 3),
            "p90": round(pct(0.9), 3),
            "max": round(sorted_g[-1], 3),
        }

    @staticmethod
    def _normalized_entropy(counts: Counter) -> float:
        """Shannon entropy of turn distribution, normalized to [0, 1]."""
        n_speakers = len(counts)
        total = sum(counts.values())
        if n_speakers <= 1 or total == 0:
            return 0.0
        entropy = 0.0
        for c in counts.values():
            p = c / total
            if p > 0:
                entropy -= p * math.log(p)
        max_entropy = math.log(n_speakers)
        return round(entropy / max_entropy, 4) if max_entropy > 0 else 0.0

    @staticmethod
    def _longest_by_speaker(turns: Sequence[Dict[str, Any]]) -> Dict[str, float]:
        """Longest turn duration per speaker_id."""
        longest: Dict[str, float] = defaultdict(float)
        for t in turns:
            sid = t["speaker_id"] or "__unknown__"
            longest[sid] = max(longest[sid], t["end"] - t["start"])
        return dict(longest)

    @staticmethod
    def _lsm(a_tokens: Sequence[str], b_tokens: Sequence[str]) -> Optional[float]:
        """Compute Linguistic Style Matching across 9 function-word categories.

        Returns ``None`` when either side has too few tokens for the
        result to be stable (<100 per Pennebaker's stability guidance).
        Mean across categories of ``1 - |fa - fb| / (fa + fb + ε)``.
        """
        if len(a_tokens) < 100 or len(b_tokens) < 100:
            return None
        a_counts = _category_counts(a_tokens)
        b_counts = _category_counts(b_tokens)
        a_total = max(len(a_tokens), 1)
        b_total = max(len(b_tokens), 1)
        scores: List[float] = []
        for cat in _LSM_CATEGORIES:
            fa = a_counts[cat] / a_total
            fb = b_counts[cat] / b_total
            scores.append(1.0 - abs(fa - fb) / (fa + fb + 1e-6))
        return round(sum(scores) / len(scores), 4)

    @staticmethod
    def _pronoun_ratio(
        a_tokens: Sequence[str],
        b_tokens: Sequence[str],
    ) -> Dict[str, Optional[float]]:
        """'We'/'I' ratio per speaker — inclusive-speech marker."""

        def ratio(tokens: Sequence[str]) -> Optional[float]:
            we = sum(1 for t in tokens if t in {"we", "us", "our", "ours"})
            i = sum(1 for t in tokens if t in {"i", "me", "my", "mine"})
            if we + i == 0:
                return None
            # +1 smoothing so a single call doesn't produce inf.
            return round((we + 1) / (i + 1), 3)

        return {"agent": ratio(a_tokens), "customer": ratio(b_tokens)}

    @staticmethod
    def _empty(base: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "talk_pct": {"agent": 0.0, "customer": 0.0, "silence": 0.0},
            "filler_rate_per_min": {"agent": 0.0},
            "question_rate_per_min": 0.0,
            "words_per_min": {},
            "interruption_count_total": 0,
            "longest_monologue_sec": {"agent": 0.0, "customer": 0.0},
            "longest_customer_story_sec": 0.0,
            "interactivity_per_min": 0.0,
            "patience_sec": 0.0,
            "turn_entropy": 0.0,
            "back_channel_rate_per_min": {"agent": 0.0, "customer": 0.0},
            "pause_distribution_sec": {"p50": 0.0, "p90": 0.0, "max": 0.0},
            "laughter_events": 0,
            "linguistic_style_match": None,
            "pronoun_ratio_we_i": {"agent": None, "customer": None},
            "stakeholder_count": 0,
            "total_duration_sec": 0.0,
            "total_turns": 0,
        }
