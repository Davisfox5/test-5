"""Weak-supervision framework — Snorkel-style labeling functions + model.

Labeling functions (LFs) are lightweight heuristics (regex, embedding
similarity, LLM probes) that vote a *label* on each item.  The
:class:`LabelModel` combines their votes into a single probabilistic
prediction, estimating each LF's accuracy from the agreement structure
among LFs — no ground-truth labels required.

The implementation below is a compact variant of the FlyingSquid /
Snorkel MeTaL approach that produces per-LF accuracy estimates from
pairwise agreement rates and aggregates via weighted majority.

Shipped LFs cover three high-leverage signals that the pipeline
currently guesses with an LLM:

- **cancel_intent** — "we're cancelling", "end the contract", "not
  renewing", "terminate"
- **commitment_language** — "let's do it", "we'll go ahead", "sign me
  up", "I'll take it"
- **objection_resolved** — did the rep acknowledge and rebut the
  raised objection within N turns

Each LF returns an ``LFVote`` with value in {-1 (abstain), 0 (negative),
1 (positive)} and a coarse confidence.  The :class:`LabelModel`
aggregates across LFs per item.

Two usage patterns:

1. **Pipeline augmentation** — run LFs on the transcript, store the
   aggregated label in ``InteractionFeatures.llm_structured`` under an
   explicit key (e.g. ``"cancel_intent_ws"``).  The orchestrator treats
   this as an orthogonal signal to the LLM's categorical guess.
2. **Golden-set seeding** — when ≥3 LFs agree with high confidence, the
   item is added to a soft-labeled training set used for Platt
   calibration without any human correction.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


# ── Contract ─────────────────────────────────────────────────────────────


ABSTAIN = -1


@dataclass
class LFVote:
    """One labeling function's vote on a single item."""

    label: int  # ABSTAIN (-1), 0, 1 — binary LFs for now
    confidence: float = 0.5  # 0–1

    def is_vote(self) -> bool:
        return self.label != ABSTAIN


@dataclass
class LabelingFunction:
    """A named heuristic.  ``fn`` takes arbitrary item kwargs and votes."""

    name: str
    fn: Callable[..., LFVote]
    estimated_accuracy: float = 0.7  # prior; updated by the label model

    def vote(self, **item: Any) -> LFVote:
        try:
            return self.fn(**item)
        except Exception:  # noqa: BLE001 — broken LF must not kill the run
            return LFVote(label=ABSTAIN, confidence=0.0)


# ── Shipped LFs for cancel-intent / commitment / objection-resolved ──────


_CANCEL_PATTERNS = [
    r"\b(?:cancel(?:l)?(?:ing|ation)?)\b",
    r"\b(?:end(?:ing)?\s+(?:our|the|my)\s+contract)\b",
    r"\b(?:terminate|termination)\b",
    r"\b(?:not\s+(?:going\s+to\s+)?renew(?:ing)?)\b",
    r"\b(?:switch(?:ing)?\s+(?:to|back\s+to|away\s+from))\b",
    r"\b(?:won't\s+be\s+(?:renewing|continuing))\b",
    r"\b(?:close\s+(?:our|the|my)\s+account)\b",
]
_CANCEL_RE = re.compile("|".join(_CANCEL_PATTERNS), re.IGNORECASE)

_COMMITMENT_PATTERNS = [
    r"\b(?:sign\s+me\s+up)\b",
    r"\b(?:let's\s+do\s+(?:it|this))\b",
    r"\b(?:we'?ll?\s+(?:go|move)\s+(?:ahead|forward))\b",
    r"\b(?:i'?ll?\s+take\s+(?:it|that))\b",
    r"\b(?:send\s+(?:over\s+)?(?:the\s+)?(?:contract|paperwork|agreement))\b",
    r"\b(?:we'?re\s+ready\s+to\s+(?:go|buy|sign))\b",
    r"\b(?:green[-\s]?light(?:ed|ing)?)\b",
]
_COMMITMENT_RE = re.compile("|".join(_COMMITMENT_PATTERNS), re.IGNORECASE)

_OBJECTION_MARKERS = re.compile(
    r"\b(?:too\s+expensive|concerned\s+about|worried\s+that|the\s+problem\s+is|"
    r"not\s+sure|doesn'?t\s+work|can'?t\s+afford|price\s+is\s+too)\b",
    re.IGNORECASE,
)
_RESOLUTION_MARKERS = re.compile(
    r"\b(?:that\s+makes\s+sense|understood|fair\s+point|i\s+see\s+what\s+you\s+mean|"
    r"thanks\s+for\s+(?:clarifying|explaining)|you'?re\s+right)\b",
    re.IGNORECASE,
)


def lf_cancel_intent(*, transcript: str = "", **_: Any) -> LFVote:
    """Fires positive when cancellation phrases appear in the transcript."""
    matches = _CANCEL_RE.findall(transcript or "")
    if not matches:
        return LFVote(label=0, confidence=0.6)
    return LFVote(label=1, confidence=min(1.0, 0.5 + 0.2 * len(matches)))


def lf_commitment_language(*, transcript: str = "", **_: Any) -> LFVote:
    matches = _COMMITMENT_RE.findall(transcript or "")
    if not matches:
        return LFVote(label=0, confidence=0.6)
    return LFVote(label=1, confidence=min(1.0, 0.55 + 0.2 * len(matches)))


def lf_objection_resolved(*, turns: Sequence[Dict[str, Any]] = (), **_: Any) -> LFVote:
    """Positive when an objection is raised and acknowledged in the next
    3 turns by the other speaker.  Abstains when there is no objection.
    """
    if not turns:
        return LFVote(label=ABSTAIN, confidence=0.0)
    objection_at: Optional[int] = None
    objection_speaker: Optional[str] = None
    for i, t in enumerate(turns):
        text = t.get("text", "")
        if objection_at is None and _OBJECTION_MARKERS.search(text or ""):
            objection_at = i
            objection_speaker = t.get("speaker_id")
            continue
        if objection_at is not None and i - objection_at <= 3:
            if (
                t.get("speaker_id") != objection_speaker
                and _RESOLUTION_MARKERS.search(text or "")
            ):
                return LFVote(label=1, confidence=0.7)
    if objection_at is None:
        return LFVote(label=ABSTAIN, confidence=0.0)
    return LFVote(label=0, confidence=0.55)


def lf_llm_sentiment_agreement(*, llm_churn_signal: Optional[str] = None, **_: Any) -> LFVote:
    """Second opinion on churn_intent using the LLM's categorical signal."""
    if llm_churn_signal is None:
        return LFVote(label=ABSTAIN, confidence=0.0)
    if str(llm_churn_signal).lower() in {"high", "medium"}:
        return LFVote(label=1, confidence=0.6)
    return LFVote(label=0, confidence=0.6)


# ── Label model ──────────────────────────────────────────────────────────


@dataclass
class AggregatedLabel:
    label: Optional[int]  # None when all LFs abstain
    probability: float    # P(label == 1); 0.5 when ambiguous
    support: int          # number of non-abstaining LFs
    lf_votes: Dict[str, int] = field(default_factory=dict)


class LabelModel:
    """Pairwise-agreement label model that estimates LF accuracies.

    Produces a probabilistic label per item by combining LF votes
    weighted by each LF's estimated accuracy.  Accuracies come from the
    fraction of times an LF agrees with the majority on items where at
    least two LFs vote.
    """

    def __init__(self, lfs: Sequence[LabelingFunction]) -> None:
        self._lfs = list(lfs)
        self._accuracies: Dict[str, float] = {
            lf.name: lf.estimated_accuracy for lf in lfs
        }

    # ── Accuracy estimation ───────────────────────────────────────────

    def fit(self, votes_per_item: Sequence[Dict[str, LFVote]]) -> None:
        """Update per-LF accuracies using agreement-with-consensus."""
        agreements: Dict[str, Tuple[int, int]] = {
            lf.name: (0, 0) for lf in self._lfs
        }
        for votes in votes_per_item:
            supporting = [v.label for v in votes.values() if v.is_vote()]
            if len(supporting) < 2:
                continue
            consensus = 1 if sum(supporting) > len(supporting) / 2 else 0
            for name, v in votes.items():
                if not v.is_vote():
                    continue
                hit, total = agreements[name]
                total += 1
                if v.label == consensus:
                    hit += 1
                agreements[name] = (hit, total)
        for name, (hit, total) in agreements.items():
            if total >= 5:
                # Shrink lightly toward the prior 0.7 to avoid overfitting tiny n.
                raw = hit / total
                self._accuracies[name] = round(0.2 * 0.7 + 0.8 * raw, 4)

    def accuracy(self, lf_name: str) -> float:
        return self._accuracies.get(lf_name, 0.7)

    # ── Aggregation ───────────────────────────────────────────────────

    def aggregate(self, votes: Dict[str, LFVote]) -> AggregatedLabel:
        """Combine LF votes into one probabilistic label for an item."""
        supporting = {n: v for n, v in votes.items() if v.is_vote()}
        if not supporting:
            return AggregatedLabel(label=None, probability=0.5, support=0)

        # Weighted log-odds: each voting LF adds log(acc / (1-acc)) in
        # the direction of its label, scaled by its self-reported
        # confidence.  This mirrors the naive-Bayes combiner Snorkel
        # uses when LFs are treated as conditionally independent.
        log_odds = 0.0
        for name, v in supporting.items():
            acc = max(0.51, min(0.99, self._accuracies.get(name, 0.7)))
            weight = math.log(acc / (1 - acc)) * max(0.1, v.confidence)
            log_odds += weight if v.label == 1 else -weight
        prob = 1.0 / (1.0 + math.exp(-log_odds))
        label = 1 if prob >= 0.5 else 0
        return AggregatedLabel(
            label=label,
            probability=round(prob, 4),
            support=len(supporting),
            lf_votes={n: v.label for n, v in supporting.items()},
        )


# ── Convenience constructors ─────────────────────────────────────────────


def churn_intent_model() -> LabelModel:
    """Standard churn-intent ensemble: keyword + LLM-signal + resolution."""
    return LabelModel([
        LabelingFunction("cancel_keywords", lf_cancel_intent, 0.82),
        LabelingFunction("llm_churn_signal", lf_llm_sentiment_agreement, 0.78),
    ])


def commitment_model() -> LabelModel:
    return LabelModel([
        LabelingFunction("commitment_keywords", lf_commitment_language, 0.80),
    ])


def objection_resolution_model() -> LabelModel:
    return LabelModel([
        LabelingFunction("objection_turn_sequence", lf_objection_resolved, 0.72),
    ])


# ── Convenience: apply an ensemble to one interaction's features ─────────


def label_interaction(
    *,
    transcript: str,
    turns: Sequence[Dict[str, Any]] = (),
    llm_churn_signal: Optional[str] = None,
) -> Dict[str, AggregatedLabel]:
    """Run all three shipped LFs on one interaction and return their
    aggregated labels.  Returned dict keys: ``cancel_intent``,
    ``commitment``, ``objection_resolved``.
    """
    cancel = churn_intent_model()
    commitment = commitment_model()
    objection = objection_resolution_model()

    cancel_votes = {
        lf.name: lf.vote(transcript=transcript, llm_churn_signal=llm_churn_signal)
        for lf in cancel._lfs  # noqa: SLF001 — internal access is fine here
    }
    commitment_votes = {
        lf.name: lf.vote(transcript=transcript)
        for lf in commitment._lfs  # noqa: SLF001
    }
    objection_votes = {
        lf.name: lf.vote(turns=turns)
        for lf in objection._lfs  # noqa: SLF001
    }
    return {
        "cancel_intent": cancel.aggregate(cancel_votes),
        "commitment": commitment.aggregate(commitment_votes),
        "objection_resolved": objection.aggregate(objection_votes),
    }
