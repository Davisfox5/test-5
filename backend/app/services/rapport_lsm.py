"""Linguistic Style Matching — rapport signal from the transcript alone.

LSM (Pennebaker, *Mind in the Words*) measures how closely two speakers
mirror each other's *function-word* usage — articles, pronouns,
prepositions, conjunctions, auxiliary verbs, quantifiers, negations,
adverbs. Function words are processed below conscious awareness, so
their convergence is a strong proxy for rapport / coordination /
alignment in conversation.

Formula per category::

    LSM(c) = 1 − |p1c − p2c| / (p1c + p2c + ε)

where ``p_ic`` is the proportion of speaker *i*'s words that fall in
category *c*. Averaging across categories gives a 0–1 rapport score.

This module is deliberately self-contained — no LIWC dictionary
dependency (LIWC is licensed). The function-word lists below cover the
major categories well enough for a directional signal; replacing them
with the licensed LIWC categories is a drop-in upgrade.

The rapport gauge composite the LLM-emitted "trust signals" /
"commitment language" with this deterministic LSM score downstream;
this module's job is just to compute LSM cleanly.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ── Function-word categories ─────────────────────────────────────────
# Curated from the public Pennebaker function-word list. Not a full
# LIWC replacement; covers ~90% of the volume in English conversation.

FUNCTION_WORDS: Dict[str, set] = {
    "articles": {"a", "an", "the"},
    "personal_pronouns": {
        "i", "me", "my", "mine", "myself",
        "we", "us", "our", "ours", "ourselves",
        "you", "your", "yours", "yourself", "yourselves",
        "he", "him", "his", "himself",
        "she", "her", "hers", "herself",
        "it", "its", "itself",
        "they", "them", "their", "theirs", "themselves",
    },
    "prepositions": {
        "of", "in", "to", "for", "with", "on", "at", "by", "from",
        "about", "into", "through", "during", "before", "after",
        "above", "below", "between", "under", "over", "against",
        "without", "within", "across", "behind", "beyond", "despite",
        "except", "near", "off", "onto", "since", "toward", "towards",
        "upon",
    },
    "conjunctions": {
        "and", "but", "or", "so", "because", "although", "though",
        "while", "whereas", "if", "unless", "until", "when", "whenever",
        "where", "wherever", "as", "than", "yet", "nor",
    },
    "auxiliary_verbs": {
        "am", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "having",
        "do", "does", "did", "doing",
        "will", "would", "shall", "should", "can", "could", "may",
        "might", "must", "ought",
    },
    "quantifiers": {
        "some", "any", "all", "no", "every", "many", "much", "few",
        "several", "more", "most", "less", "least", "enough", "both",
        "either", "neither", "each",
    },
    "negations": {
        "not", "no", "never", "none", "nothing", "nobody", "nowhere",
        "neither", "nor",
    },
    "adverbs_high_freq": {
        "very", "just", "really", "actually", "still", "already",
        "almost", "even", "again", "always", "often", "sometimes",
        "rather", "quite", "too", "also",
    },
}

# A small additive constant keeps the denominator nonzero when neither
# speaker emitted any words in the category. Pennebaker uses 0.0001;
# we match.
LSM_EPSILON = 0.0001


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    return re.findall(r"[a-zA-Z']+", text.lower())


def _category_proportions(tokens: Iterable[str]) -> Tuple[Dict[str, float], int]:
    """Return ``({category: proportion_of_total_words}, total_words)``.

    Proportions are over total *tokenized* words, not over function
    words only — that's the Pennebaker convention.
    """
    counts: Dict[str, int] = {c: 0 for c in FUNCTION_WORDS}
    total = 0
    token_list = list(tokens)
    total = len(token_list)
    if total == 0:
        return ({c: 0.0 for c in FUNCTION_WORDS}, 0)
    for tok in token_list:
        for cat, vocab in FUNCTION_WORDS.items():
            if tok in vocab:
                counts[cat] += 1
    return ({c: counts[c] / total for c in FUNCTION_WORDS}, total)


def compute_lsm_pair(text_a: str, text_b: str) -> Optional[Dict[str, float]]:
    """Compute LSM between two aggregated speaker texts.

    Returns ``None`` when either side has zero words — LSM is undefined
    against an empty contribution and we don't want to fake a number.

    The output dict has one key per function-word category plus
    ``overall`` (the mean across categories).
    """
    p_a, total_a = _category_proportions(_tokenize(text_a))
    p_b, total_b = _category_proportions(_tokenize(text_b))
    if total_a == 0 or total_b == 0:
        return None
    out: Dict[str, float] = {}
    for cat in FUNCTION_WORDS:
        a = p_a[cat]
        b = p_b[cat]
        denom = a + b + LSM_EPSILON
        out[cat] = round(1 - abs(a - b) / denom, 4)
    out["overall"] = round(sum(out.values()) / len(FUNCTION_WORDS), 4)
    return out


def _classify_role(speaker: Optional[str], role: Optional[str]) -> str:
    """Map a transcript turn to ``rep`` / ``customer`` / ``other``.

    Backend transcripts use mixed conventions — sometimes ``role``
    is set explicitly (``rep`` / ``customer`` / ``agent``), sometimes
    only ``speaker`` is set with vendor-specific labels (``Speaker A``,
    ``agent_1``, ``Customer``). We do best-effort classification and
    fall back to ``other`` so the LSM computation excludes IVR /
    voicemail / system messages.
    """
    for source in (role, speaker):
        if not source:
            continue
        s = str(source).lower()
        if "customer" in s or "caller" in s or "client" in s:
            return "customer"
        if "rep" in s or "agent" in s or "vendor" in s or "host" in s:
            return "rep"
    # ``Speaker A`` / ``Speaker B`` heuristic: when nothing else is
    # available, take the first label encountered as rep, second as
    # customer. Caller resolves the ordering at call sites.
    return "other"


def compute_lsm_for_transcript(
    transcript: List[Dict[str, Any]],
) -> Optional[Dict[str, float]]:
    """Aggregate transcript turns by side then compute LSM.

    Pass-through tolerates the half-dozen transcript shapes the
    pipeline produces (asr-vendor specific, ingest-fallback, manual
    upload). When we can't pull two distinct speakers, returns ``None``
    so the UI can hide the gauge rather than render a meaningless 0.
    """
    if not transcript:
        return None
    rep_chunks: List[str] = []
    cust_chunks: List[str] = []
    # Fallback rolling assignment for unlabeled "Speaker A / B" calls:
    # the first distinct label seen wins "rep", the second "customer".
    fallback_label_to_role: Dict[str, str] = {}
    for turn in transcript:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        role = _classify_role(turn.get("speaker"), turn.get("role"))
        if role == "other":
            label = str(turn.get("speaker") or "").strip().lower()
            if label:
                if label not in fallback_label_to_role:
                    if "rep" not in fallback_label_to_role.values():
                        fallback_label_to_role[label] = "rep"
                    elif "customer" not in fallback_label_to_role.values():
                        fallback_label_to_role[label] = "customer"
                    else:
                        fallback_label_to_role[label] = "other"
                role = fallback_label_to_role[label]
        if role == "rep":
            rep_chunks.append(text)
        elif role == "customer":
            cust_chunks.append(text)
    if not rep_chunks or not cust_chunks:
        return None
    return compute_lsm_pair(" ".join(rep_chunks), " ".join(cust_chunks))


def attach_rapport(insights: Dict[str, Any], transcript: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Compute LSM from the transcript and write it to
    ``insights['rapport']``. Returns the rapport dict or None.

    Composes with the existing ``trust_signals`` /
    ``commitment_language`` lists the LLM emits in
    ``insights['customer_signals']``: those are evidence of *outcomes*
    that suggest rapport, while LSM is a *measurement* of the
    coordination process itself. Writing both to ``insights`` lets the
    UI render a composite gauge.
    """
    lsm = compute_lsm_for_transcript(transcript)
    if lsm is None:
        return None
    insights["rapport"] = {
        "lsm_overall": lsm["overall"],
        "lsm_by_category": {k: v for k, v in lsm.items() if k != "overall"},
        "overall": lsm["overall"],
    }
    return insights["rapport"]


# ── Vocal accommodation (Phase 2) ────────────────────────────────────


# Features the rep / customer convergence is measured on. Each feature
# yields its own 0..1 score; the overall is the mean. Keys match the
# extractor's ``_measure_slices`` output (paralinguistics.py).
_VOCAL_ACCOM_FEATURES: Tuple[str, ...] = (
    "pitch_hz_p50",
    "intensity_db_p50",
    "speaking_rate_syll_per_sec",
    "pause_rate_per_min",
)


def _identify_speaker_roles(per_speaker: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Pick rep + customer keys out of the paralinguistic block.

    Same role-classification heuristic as ``compute_lsm_for_transcript``:
    explicit ``rep`` / ``customer`` labels first, fallback to the first
    two distinct keys we see when the labels are vendor-specific
    (``Speaker A`` / ``Speaker B``).
    """
    rep_key: Optional[str] = None
    cust_key: Optional[str] = None
    leftover: List[str] = []
    for key in per_speaker.keys():
        role = _classify_role(key, None)
        if role == "rep" and rep_key is None:
            rep_key = key
        elif role == "customer" and cust_key is None:
            cust_key = key
        else:
            leftover.append(key)
    # Fallback assignment when classification missed both sides.
    if rep_key is None and leftover:
        rep_key = leftover.pop(0)
    if cust_key is None and leftover:
        cust_key = leftover.pop(0)
    return rep_key, cust_key


def compute_vocal_accommodation(
    paralinguistic_block: Optional[Dict[str, Any]],
) -> Optional[Dict[str, float]]:
    """Compute a 0..1 accommodation score from per-speaker prosody.

    For each feature in ``_VOCAL_ACCOM_FEATURES``, score
    ``1 − |rep − cust| / (|rep| + |cust| + ε)`` — same shape as LSM.
    A perfectly mirrored pair lands at 1.0; opposite poles → 0.0.
    Returns ``None`` when fewer than two speakers are present or when
    none of the tracked features have values for both speakers.
    """
    if not paralinguistic_block or not paralinguistic_block.get("available"):
        return None
    per_speaker = paralinguistic_block.get("per_speaker") or {}
    if len(per_speaker) < 2:
        return None
    rep_key, cust_key = _identify_speaker_roles(per_speaker)
    if not rep_key or not cust_key:
        return None
    rep_block = per_speaker.get(rep_key) or {}
    cust_block = per_speaker.get(cust_key) or {}
    out: Dict[str, float] = {}
    for feat in _VOCAL_ACCOM_FEATURES:
        rep_v = rep_block.get(feat)
        cust_v = cust_block.get(feat)
        if rep_v is None or cust_v is None:
            continue
        try:
            rep_f = float(rep_v)
            cust_f = float(cust_v)
        except (TypeError, ValueError):
            continue
        denom = abs(rep_f) + abs(cust_f) + 1e-6
        out[feat] = round(1 - abs(rep_f - cust_f) / denom, 4)
    if not out:
        return None
    out["overall"] = round(sum(out.values()) / len(out), 4)
    return out


def attach_vocal_accommodation(
    insights: Dict[str, Any],
    paralinguistic_block: Optional[Dict[str, Any]],
) -> Optional[Dict[str, float]]:
    """Compute vocal accommodation and merge it into ``insights['rapport']``.

    Updates the composite ``overall`` to be the mean of LSM and vocal
    accommodation when both are present, falling back to whichever side
    is available. Returns the accommodation sub-dict (not the merged
    rapport). No-op when paralinguistic block is unavailable, but does
    NOT delete the existing LSM-only rapport block in that case.
    """
    accom = compute_vocal_accommodation(paralinguistic_block)
    if accom is None:
        return None
    rapport = insights.setdefault("rapport", {})
    rapport["vocal_accommodation"] = accom
    lsm_overall = rapport.get("lsm_overall")
    if isinstance(lsm_overall, (int, float)):
        rapport["overall"] = round(
            (float(lsm_overall) + float(accom["overall"])) / 2, 4
        )
    else:
        rapport["overall"] = accom["overall"]
    return accom
