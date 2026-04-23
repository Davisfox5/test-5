"""Deterministic real-time coaching — zero-LLM-cost sliding-window features.

Designed to run inside the existing WebSocket live-call handler with
sub-second latency.  Every computation here is a pure function of the
transcript turns accumulated so far; no network calls.

Surfaces two primitives used by the live coaching UI:

- :class:`LiveFeatureWindow` — maintains the last ``window_sec`` of
  diarized turns and exposes rolling talk/listen, patience,
  interactivity, filler rate, question rate, and LSM-so-far.
- :class:`LFTriggerScanner` — runs the cancel-intent and commitment-
  language labeling functions on every new customer turn and emits
  :class:`CoachingAlert` objects when they fire.

Both are intended to be owned by the WebSocket connection (one per
call) and fed turn-by-turn.  The handler in ``api/websocket.py`` (or a
successor) is responsible for serializing alerts to the client.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence

from backend.app.services.feature_extractors import (
    _LSM_CATEGORIES,
    _category_counts,
    _looks_like_backchannel,
    _tokens,
)
from backend.app.services.weak_supervision import (
    LFVote,
    lf_cancel_intent,
    lf_commitment_language,
)


# ── Shared types ─────────────────────────────────────────────────────────


@dataclass
class LiveTurn:
    speaker_id: str
    text: str
    start: float
    end: float
    is_agent: bool


@dataclass
class CoachingAlert:
    """One event to render as a card in the agent UI.

    ``kind`` is a coarse category the UI styles against:
    ``patience`` (agent interrupting), ``monologue`` (agent talking too
    long), ``filler`` (filler spike), ``cancel_intent`` (customer
    signalling churn), ``commitment`` (customer committing to buy),
    ``silence`` (long customer pause), ``rapport`` (back-channel gap).
    ``severity`` is ``info | warn | alert``.
    """

    kind: str
    severity: str
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_wire(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "evidence": self.evidence,
            "t": self.created_at,
        }


# ── Sliding-window features ──────────────────────────────────────────────


_FILLER_TOKENS = {"um", "uh", "like", "basically", "actually", "you know", "i mean"}


@dataclass
class LiveFeatureSnapshot:
    """Rolling feature snapshot — consumed by the UI's always-on strip."""

    window_sec: float
    rep_talk_pct: float
    customer_talk_pct: float
    silence_pct: float
    patience_sec: Optional[float]
    interactivity_per_min: float
    filler_rate_per_min: float
    question_rate_per_min: float
    lsm_partial: Optional[float]
    back_channel_gap_sec: Optional[float]

    def to_wire(self) -> Dict[str, Any]:
        return {
            "window_sec": round(self.window_sec, 1),
            "rep_talk_pct": round(self.rep_talk_pct, 3),
            "customer_talk_pct": round(self.customer_talk_pct, 3),
            "silence_pct": round(self.silence_pct, 3),
            "patience_sec": (
                round(self.patience_sec, 3) if self.patience_sec is not None else None
            ),
            "interactivity_per_min": round(self.interactivity_per_min, 2),
            "filler_rate_per_min": round(self.filler_rate_per_min, 2),
            "question_rate_per_min": round(self.question_rate_per_min, 2),
            "lsm_partial": (
                round(self.lsm_partial, 3) if self.lsm_partial is not None else None
            ),
            "back_channel_gap_sec": (
                round(self.back_channel_gap_sec, 2)
                if self.back_channel_gap_sec is not None
                else None
            ),
        }


class LiveFeatureWindow:
    """Bounded deque of recent turns + rolling feature snapshots.

    Turns older than ``window_sec`` are trimmed on every push so the
    memory footprint stays O(turns in window).  Snapshots are cheap
    (linear in turns-in-window) — compute them on every client-polled
    tick or every N pushed turns.
    """

    def __init__(self, window_sec: float = 60.0) -> None:
        self.window_sec = window_sec
        self._turns: Deque[LiveTurn] = deque()
        self._last_customer_turn_end: Optional[float] = None
        self._last_agent_utterance_after_customer: Optional[float] = None

    # ── Mutators ──────────────────────────────────────────────────────

    def push(self, turn: LiveTurn) -> None:
        # Trim anything that ended before the start of the rolling window.
        cutoff = turn.end - self.window_sec
        while self._turns and self._turns[0].end < cutoff:
            self._turns.popleft()
        self._turns.append(turn)

    # ── Snapshot ──────────────────────────────────────────────────────

    def snapshot(self) -> LiveFeatureSnapshot:
        if not self._turns:
            return LiveFeatureSnapshot(
                window_sec=0.0,
                rep_talk_pct=0.0,
                customer_talk_pct=0.0,
                silence_pct=0.0,
                patience_sec=None,
                interactivity_per_min=0.0,
                filler_rate_per_min=0.0,
                question_rate_per_min=0.0,
                lsm_partial=None,
                back_channel_gap_sec=None,
            )
        first = self._turns[0].start
        last = self._turns[-1].end
        total = max(last - first, 1e-6)
        rep_time = sum(
            max(t.end - t.start, 0.0) for t in self._turns if t.is_agent
        )
        cust_time = sum(
            max(t.end - t.start, 0.0) for t in self._turns if not t.is_agent
        )
        silence = max(total - rep_time - cust_time, 0.0)

        patience = self._median_patience_sec()
        switches = sum(
            1 for i in range(1, len(self._turns))
            if self._turns[i].speaker_id != self._turns[i - 1].speaker_id
        )
        minutes = total / 60.0
        interactivity = switches / minutes if minutes > 0 else 0.0

        agent_words = 0
        agent_fillers = 0
        agent_questions = 0
        agent_tokens: List[str] = []
        cust_tokens: List[str] = []
        for t in self._turns:
            tokens = _tokens(t.text)
            if t.is_agent:
                agent_words += len(tokens)
                agent_tokens.extend(tokens)
                text_lower = (t.text or "").lower()
                agent_fillers += sum(text_lower.count(f) for f in _FILLER_TOKENS)
                agent_questions += t.text.count("?") if t.text else 0
            else:
                cust_tokens.extend(tokens)

        filler_rate = agent_fillers / minutes if minutes > 0 else 0.0
        question_rate = agent_questions / minutes if minutes > 0 else 0.0
        lsm = _lsm_partial(agent_tokens, cust_tokens)
        back_channel_gap = self._back_channel_gap_sec()

        return LiveFeatureSnapshot(
            window_sec=total,
            rep_talk_pct=rep_time / total,
            customer_talk_pct=cust_time / total,
            silence_pct=silence / total,
            patience_sec=patience,
            interactivity_per_min=interactivity,
            filler_rate_per_min=filler_rate,
            question_rate_per_min=question_rate,
            lsm_partial=lsm,
            back_channel_gap_sec=back_channel_gap,
        )

    # ── Internal ──────────────────────────────────────────────────────

    def _median_patience_sec(self) -> Optional[float]:
        """Median gap before the agent's turn after a customer turn ends."""
        gaps: List[float] = []
        for i in range(1, len(self._turns)):
            prev, curr = self._turns[i - 1], self._turns[i]
            if not prev.is_agent and curr.is_agent and curr.start >= prev.end:
                gaps.append(curr.start - prev.end)
        if not gaps:
            return None
        return float(sorted(gaps)[len(gaps) // 2])

    def _back_channel_gap_sec(self) -> Optional[float]:
        """Time elapsed inside the current customer monologue without any
        agent back-channel acknowledgement.

        Used by the UI to whisper "Say 'I hear you'" when the customer
        has been talking alone for too long.
        """
        # Walk backward until we hit the last agent utterance.  Customer
        # turns accumulated since then count toward the gap.
        gap = 0.0
        for t in reversed(self._turns):
            if t.is_agent:
                # If the last agent utterance looked like a back-channel,
                # the gap is zero — we've been acknowledging the customer.
                if _looks_like_backchannel(t.text):
                    return 0.0
                return gap if gap > 0 else None
            gap += max(t.end - t.start, 0.0)
        # No agent turns in the window at all → the gap is the full
        # customer stretch.
        return gap if gap > 0 else None


def _lsm_partial(a_tokens: Sequence[str], b_tokens: Sequence[str]) -> Optional[float]:
    """Partial-LSM: only computes when both sides have ≥30 function-word
    tokens.  The full 100-token threshold is relaxed for live use since
    we only have the last 60s; this is a coarser but still useful signal.
    """
    if len(a_tokens) < 30 or len(b_tokens) < 30:
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
    return sum(scores) / len(scores)


# ── LF trigger scanner ───────────────────────────────────────────────────


# Per-call thresholds — chosen to surface alerts without spam.  Tunable
# per-tenant in a follow-up PR via ``TenantPromptConfig.parameter_overrides``.
_THRESHOLDS = {
    "monologue_sec": 60.0,
    "filler_spike_per_min": 12.0,
    "patience_low_sec": 0.25,
    "back_channel_gap_sec": 20.0,
}


@dataclass
class _Cooldown:
    """Prevent alert spam — track when each kind last fired."""

    last_fired: Dict[str, float] = field(default_factory=dict)
    min_interval_sec: float = 30.0

    def should_fire(self, kind: str, now: float) -> bool:
        prev = self.last_fired.get(kind)
        if prev is None or now - prev >= self.min_interval_sec:
            self.last_fired[kind] = now
            return True
        return False


class LFTriggerScanner:
    """Runs the deterministic LFs after every new customer turn.

    Keeps its own cooldown state so the same alert doesn't fire every
    second.  Returns a list of :class:`CoachingAlert` on every push —
    empty when nothing of note happened.
    """

    def __init__(self, cooldown_sec: float = 30.0) -> None:
        self._cooldown = _Cooldown(min_interval_sec=cooldown_sec)
        self._current_agent_monologue_start: Optional[float] = None
        self._running_transcript: List[LiveTurn] = []

    def push(
        self,
        turn: LiveTurn,
        window: LiveFeatureWindow,
    ) -> List[CoachingAlert]:
        self._running_transcript.append(turn)
        if len(self._running_transcript) > 500:  # safety cap — 500 turns ≈ long call
            self._running_transcript.pop(0)

        alerts: List[CoachingAlert] = []
        now = time.time()

        # Monologue detection — only agent side.
        if turn.is_agent:
            if self._current_agent_monologue_start is None:
                self._current_agent_monologue_start = turn.start
            mono_len = turn.end - self._current_agent_monologue_start
            if (
                mono_len > _THRESHOLDS["monologue_sec"]
                and self._cooldown.should_fire("monologue", now)
            ):
                alerts.append(CoachingAlert(
                    kind="monologue",
                    severity="warn",
                    message="You've been talking for over a minute — let them respond.",
                    evidence={"monologue_sec": round(mono_len, 1)},
                ))
        else:
            self._current_agent_monologue_start = None

        # Customer-side LF checks.
        if not turn.is_agent:
            cancel_vote = lf_cancel_intent(transcript=turn.text)
            if cancel_vote.label == 1 and self._cooldown.should_fire("cancel_intent", now):
                alerts.append(CoachingAlert(
                    kind="cancel_intent",
                    severity="alert",
                    message="Customer mentioned cancellation — pause, acknowledge, ask what's driving it.",
                    evidence={"confidence": cancel_vote.confidence},
                ))
            commit_vote = lf_commitment_language(transcript=turn.text)
            if commit_vote.label == 1 and self._cooldown.should_fire("commitment", now):
                alerts.append(CoachingAlert(
                    kind="commitment",
                    severity="info",
                    message="Commitment language detected — confirm next step on the call.",
                    evidence={"confidence": commit_vote.confidence},
                ))

        # Rolling-window signals.
        snapshot = window.snapshot()
        if (
            snapshot.filler_rate_per_min > _THRESHOLDS["filler_spike_per_min"]
            and self._cooldown.should_fire("filler", now)
        ):
            alerts.append(CoachingAlert(
                kind="filler",
                severity="info",
                message="Filler words are creeping in — slow down, breathe.",
                evidence={"filler_rate_per_min": round(snapshot.filler_rate_per_min, 1)},
            ))
        if (
            snapshot.patience_sec is not None
            and snapshot.patience_sec < _THRESHOLDS["patience_low_sec"]
            and self._cooldown.should_fire("patience", now)
        ):
            alerts.append(CoachingAlert(
                kind="patience",
                severity="warn",
                message="You've been jumping in quickly — leave space after they speak.",
                evidence={"patience_sec": round(snapshot.patience_sec, 2)},
            ))
        if (
            snapshot.back_channel_gap_sec is not None
            and snapshot.back_channel_gap_sec > _THRESHOLDS["back_channel_gap_sec"]
            and self._cooldown.should_fire("rapport", now)
        ):
            alerts.append(CoachingAlert(
                kind="rapport",
                severity="info",
                message='Customer has been going — a short "I hear you" keeps the thread alive.',
                evidence={"back_channel_gap_sec": round(snapshot.back_channel_gap_sec, 1)},
            ))

        return alerts


# ── Paralinguistic scanner ───────────────────────────────────────────────


# Thresholds chosen to match typical call-center norms (not perfectionist):
#   * pitch_std_semitones < 2.0 reads as monotone in informal listening
#   * speaking_rate > 4.5 syll/sec is noticeably fast in EN
#   * jitter > 0.02 and shimmer > 0.1 are the established "voice stress"
#     cutoffs in clinical phonetics (Teixeira et al. 2013)
#   * pause_rate_per_min > 8 flags halting speech / long silences
_PARALING_THRESHOLDS = {
    "monotone_pitch_std_semitones": 2.0,
    "fast_rate_syll_per_sec": 4.5,
    "jitter_stress": 0.02,
    "shimmer_stress": 0.1,
    "silence_pause_rate_per_min": 8.0,
}


class ParalinguisticScanner:
    """Turns :class:`ParalinguisticFeatures` snapshots into coaching alerts.

    Mirrors :class:`LFTriggerScanner`: stateless except for per-kind
    cooldowns so the UI doesn't get spammed with the same alert every
    few seconds.

    Only emits for the agent speaker_id (defaults to "agent"); customer
    acoustic signals feed into the scorers instead.
    """

    def __init__(
        self,
        *,
        cooldown_sec: float = 60.0,
        agent_speaker_id: str = "agent",
    ) -> None:
        self._cooldown = _Cooldown(min_interval_sec=cooldown_sec)
        self._agent_speaker_id = agent_speaker_id

    def push(self, features: Any) -> List[CoachingAlert]:
        """Scan one snapshot and return any alerts that fire."""
        if features is None or not getattr(features, "available", False):
            return []

        per_speaker = getattr(features, "per_speaker", {}) or {}
        # Resolution order:
        #   1. Explicit agent row (post-call pipeline with diarization).
        #   2. Aggregate "window" row (live mode — see
        #      LiveParalinguisticWindow.WHOLE_WINDOW_SPEAKER_ID).
        #   3. First per-speaker row as a last resort.
        agent = per_speaker.get(self._agent_speaker_id) or {}
        if not agent:
            agent = per_speaker.get("window") or {}
        if not agent:
            agent = next(iter(per_speaker.values()), {}) or {}

        alerts: List[CoachingAlert] = []
        now = time.time()

        pitch_std = agent.get("pitch_std_semitones")
        if (
            pitch_std is not None
            and pitch_std < _PARALING_THRESHOLDS["monotone_pitch_std_semitones"]
            and self._cooldown.should_fire("monotone", now)
        ):
            alerts.append(CoachingAlert(
                kind="monotone",
                severity="info",
                message="Your tone has gone flat — vary your pitch to re-engage.",
                evidence={"pitch_std_semitones": round(pitch_std, 2)},
            ))

        rate = agent.get("speaking_rate_syll_per_sec")
        if (
            rate is not None
            and rate > _PARALING_THRESHOLDS["fast_rate_syll_per_sec"]
            and self._cooldown.should_fire("pace", now)
        ):
            alerts.append(CoachingAlert(
                kind="pace",
                severity="warn",
                message="You're speaking quickly — slow down a quarter and let it land.",
                evidence={"speaking_rate_syll_per_sec": round(rate, 2)},
            ))

        jitter = agent.get("jitter_local") or 0.0
        shimmer = agent.get("shimmer_local") or 0.0
        if (
            (jitter > _PARALING_THRESHOLDS["jitter_stress"]
             or shimmer > _PARALING_THRESHOLDS["shimmer_stress"])
            and self._cooldown.should_fire("stress", now)
        ):
            alerts.append(CoachingAlert(
                kind="stress",
                severity="warn",
                message="Voice strain picking up — pause, take a breath, reset.",
                evidence={
                    "jitter_local": round(jitter, 4),
                    "shimmer_local": round(shimmer, 4),
                },
            ))

        pause_rate = agent.get("pause_rate_per_min")
        if (
            pause_rate is not None
            and pause_rate > _PARALING_THRESHOLDS["silence_pause_rate_per_min"]
            and self._cooldown.should_fire("silence", now)
        ):
            alerts.append(CoachingAlert(
                kind="silence",
                severity="info",
                message="Lots of long pauses — pick up the thread with a question.",
                evidence={"pause_rate_per_min": round(pause_rate, 2)},
            ))

        return alerts


__all__ = [
    "LiveTurn",
    "LiveFeatureWindow",
    "LiveFeatureSnapshot",
    "CoachingAlert",
    "LFTriggerScanner",
    "ParalinguisticScanner",
]
