"""Plain-English voice utility for manager-surface text.

Mirrors the agent-side voice rules baked into
``backend.app.services.ai_analysis`` (commits bfaa76b + 4337a54) and
extends them to manager-level outputs: anomaly titles, recommendation
rationales, BusinessProfile summaries.

Two layers:

1. ``MANAGER_VOICE_RULES`` — system-prompt fragment prepended to any
   Haiku call that writes manager-facing prose. Keeps the rules in one
   place so the agent and manager surfaces don't drift.

2. ``sanitize_manager_text`` / ``sanitize_manager_payload`` — post-hoc
   scrubber that strips em-dashes, banned phrases, and caps word counts
   so a slipped model output still lands clean. Field-aware: verbatim
   customer quotes (keys named ``quote`` and the ``customer_signals``
   subtree) are preserved.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


# ── Voice rules prepended to manager-facing Haiku prompts ──────────────
#
# Shared discipline block — every domain's voice rules start with the
# same anti-jargon / anti-em-dash / evidence-cite checklist. The per-
# domain wrappers add only the framing sentence ("You are a {coach}
# briefing a {manager}…") and the motion-specific vocabulary list.
# Backward-compat: ``MANAGER_VOICE_RULES`` remains exported and
# resolves to the sales prompt, so existing imports (the anomaly
# detector, recommendation builder, tenant-insights service) keep
# working unchanged until they're migrated to ``manager_voice_rules_for(domain)``.

_SHARED_VOICE_DISCIPLINE = (
    "VOICE RULES\n"
    "1. Lead with the observation, then the evidence. Never preamble.\n"
    "2. Cite specific call counts, customer counts, dollar amounts when "
    "available. Numbers beat adjectives.\n"
    "3. One short sentence per item. Hard caps below. Respect them.\n"
    "4. NEVER use em-dashes (—) or en-dashes (–) anywhere. Use "
    "periods, colons, commas, semicolons, or parentheses instead.\n"
    "5. Banned phrases: 'You did a great job', 'It's important to', "
    "'Remember to', 'Going forward, consider', 'This is a common', "
    "'In conclusion', 'Overall', 'It's worth noting', 'Make sure to', "
    "'I want to make sure', 'I want to ensure', 'Just to be clear'. If "
    "you find yourself reaching for these, you're being too explanatory.\n"
    "7. Neutral third person for observations. Recommendations are "
    "direct second person but still terse and specific.\n"
    "8. Never invent counts or quotes. If you don't have evidence, leave "
    "the field empty.\n"
)


MANAGER_VOICE_RULES_SALES = (
    "You are a sales coach briefing a manager on what is happening across "
    "their sales floor. Voice is clipboard notes: clean, specific, "
    "evidence-cited. Imagine a head coach writing on a notepad after "
    "watching the game tape. Get in, make the point, get out.\n\n"
    + _SHARED_VOICE_DISCIPLINE
    + "6. PLAIN ENGLISH for a non-technical sales director. No invented "
    "technical phrases or internal slang. Use the sales manager's "
    "vocabulary: 'losing customers', 'pricing pushback', 'discovery', "
    "'objection', 'follow-up', 'training gap', 'winning script'. "
    "Recommendations like 'Coach Maria on discovery' are second person.\n"
)


MANAGER_VOICE_RULES_CUSTOMER_SERVICE = (
    "You are a customer-success lead briefing a CS manager on what is "
    "happening across their accounts. Voice is clipboard notes: clean, "
    "specific, evidence-cited. Imagine a head of CS jotting after "
    "reviewing the week's calls. Get in, make the point, get out.\n\n"
    + _SHARED_VOICE_DISCIPLINE
    + "6. PLAIN ENGLISH for a non-technical CS manager. Frame everything "
    "around retention, expansion, and account health. Use the CS "
    "manager's vocabulary: 'renewal risk', 'adoption gap', 'champion "
    "silent', 'expansion signal', 'health drop', 'stuck onboarding', "
    "'QBR overdue'. Do NOT use sales vocabulary ('objection', 'closing', "
    "'pipeline') — this isn't a sales motion. Recommendations like "
    "'Schedule a QBR with Acme' or 'Flag the Northstar renewal' are "
    "second person.\n"
)


MANAGER_VOICE_RULES_IT_SUPPORT = (
    "You are a support lead briefing an IT-support manager on what is "
    "happening across their queue. Voice is clipboard notes: clean, "
    "specific, evidence-cited. Imagine a head of support jotting after "
    "reviewing the day's tickets. Get in, make the point, get out.\n\n"
    + _SHARED_VOICE_DISCIPLINE
    + "6. PLAIN ENGLISH for a non-technical support manager. Frame "
    "everything around resolution, escalation, and customer effort. "
    "Use the support manager's vocabulary: 'first-contact resolution', "
    "'time to resolve', 'escalation rate', 'CSAT drop', 'repeat caller', "
    "'recurring issue', 'KB gap', 'reroute'. Do NOT use sales or CS "
    "vocabulary ('objection', 'closing', 'expansion', 'QBR') — this "
    "isn't either motion. Recommendations like 'Update the KB article "
    "on VPN reauth' or 'Route Tier-2 tickets to Priya for the next week' "
    "are second person.\n"
)


MANAGER_VOICE_RULES_GENERIC = MANAGER_VOICE_RULES_SALES


# Backward-compat alias. Imports of ``MANAGER_VOICE_RULES`` (anomaly
# detector, recommendation builder) keep resolving to the sales prompt
# until they're migrated to ``manager_voice_rules_for(domain)``.
MANAGER_VOICE_RULES = MANAGER_VOICE_RULES_SALES


_MANAGER_VOICE_RULES_BY_DOMAIN: Dict[str, str] = {
    "sales": MANAGER_VOICE_RULES_SALES,
    "customer_service": MANAGER_VOICE_RULES_CUSTOMER_SERVICE,
    "it_support": MANAGER_VOICE_RULES_IT_SUPPORT,
    "generic": MANAGER_VOICE_RULES_GENERIC,
}


def manager_voice_rules_for(domain: Optional[str]) -> str:
    """Return the manager-facing voice-rules block for ``domain``.

    NULL / unknown values fall back to the sales prompt, the one rubric
    we have production-validated copy for. Callers rendering per-motion
    narratives, alerts, or recommendations should pass the relevant
    ``Interaction.domain`` (or the manager's selected motion tab) so
    generated prose lands in the right vocabulary.
    """
    if not domain:
        return MANAGER_VOICE_RULES_SALES
    return _MANAGER_VOICE_RULES_BY_DOMAIN.get(domain, MANAGER_VOICE_RULES_SALES)


# ── Banned phrases (case-insensitive substring match) ─────────────────

_BANNED_PHRASES = (
    "you did a great job",
    "it's important to",
    "remember to",
    "going forward, consider",
    "this is a common",
    "in conclusion",
    "overall,",
    "it's worth noting",
    "make sure to",
    "i want to make sure",
    "i want to ensure",
    "just to be clear",
)


# ── Field-aware verbatim protection ────────────────────────────────────

# Subtrees inside which em-dashes are preserved verbatim (these carry
# customer quotes, not analysis prose).
_VERBATIM_SUBTREES = {"customer_signals"}
# Individual keys whose string value is a verbatim quote.
_VERBATIM_VALUE_KEYS = {"quote", "sample_quote", "evidence_quote"}


# ── Core scrubbers ─────────────────────────────────────────────────────


def _strip_dashes(s: str) -> str:
    """Replace em-/en-dashes with periods, collapse double spaces."""
    if "—" not in s and "–" not in s:
        return s
    out = (
        s.replace(" — ", ". ")
        .replace("—", ". ")
        .replace(" – ", ". ")
        .replace("–", ". ")
    )
    while "  " in out:
        out = out.replace("  ", " ")
    return out.strip()


def _strip_banned(s: str) -> str:
    """Best-effort removal of canned banned phrases.

    Case-insensitive: we drop the phrase entirely and let the surrounding
    sentence carry the message. This is intentionally lossy: the model
    is told not to emit these in the first place; the scrub is the
    safety net for when it slips.
    """
    if not s:
        return s
    low = s.lower()
    if not any(p in low for p in _BANNED_PHRASES):
        return s
    out = s
    for phrase in _BANNED_PHRASES:
        idx = out.lower().find(phrase)
        while idx != -1:
            out = (out[:idx] + out[idx + len(phrase):]).strip()
            idx = out.lower().find(phrase)
    while "  " in out:
        out = out.replace("  ", " ")
    return out.strip(" ,.;:")


def _cap_words(s: str, max_words: int) -> str:
    """Truncate to ``max_words``, preserving sentence-end punctuation
    when we cut. No ellipsis added; this is a hard cap intended for
    output that already aims at the budget."""
    if max_words <= 0:
        return s
    parts = s.split()
    if len(parts) <= max_words:
        return s
    cut = " ".join(parts[:max_words])
    # If we cut mid-sentence, end with a period so it doesn't dangle.
    if cut and cut[-1] not in ".!?":
        cut = cut.rstrip(",;:") + "."
    return cut


def sanitize_manager_text(value: str, *, max_words: int = 25) -> str:
    """Apply the full manager-voice scrub to one string.

    Order matters: strip dashes first (it changes word boundaries),
    then banned phrases, then enforce the word cap.
    """
    if not isinstance(value, str) or not value:
        return value
    out = _strip_dashes(value)
    out = _strip_banned(out)
    out = _cap_words(out, max_words)
    return out


def sanitize_manager_payload(
    obj: Any,
    *,
    max_words_per_field: Optional[Dict[str, int]] = None,
    default_max_words: Optional[int] = None,
    verbatim_keys: Iterable[str] = (),
) -> None:
    """Recursively scrub a JSON-shaped object in place.

    Preserves verbatim quote subtrees and ``quote``-style keys so
    customer speech isn't mangled. Per-field word caps are looked up by
    key name; everything else gets the dash + banned-phrase scrub but
    no word cap unless ``default_max_words`` is provided.

    Args:
        obj: Dict / list / scalar to scrub in place.
        max_words_per_field: ``{"title": 25, "summary": 60}`` style.
        default_max_words: applied to any string field not in the map.
            None means "scrub dashes and banned phrases only".
        verbatim_keys: Additional keys (beyond the default
            ``quote``/``sample_quote``/``evidence_quote``) whose string
            value should be left untouched.
    """
    caps = max_words_per_field or {}
    extra_verbatim = set(verbatim_keys) | _VERBATIM_VALUE_KEYS

    def _scrub_str(s: str, *, max_words: Optional[int]) -> str:
        out = _strip_dashes(s)
        out = _strip_banned(out)
        if max_words is not None and max_words > 0:
            out = _cap_words(out, max_words)
        return out

    def _walk(node: Any, *, parent_key: Optional[str] = None) -> None:
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k in _VERBATIM_SUBTREES:
                    continue
                if isinstance(v, str):
                    if k in extra_verbatim:
                        continue
                    max_w = caps.get(k, default_max_words)
                    node[k] = _scrub_str(v, max_words=max_w)
                else:
                    _walk(v, parent_key=k)
        elif isinstance(node, list):
            max_w = caps.get(parent_key, default_max_words) if parent_key else default_max_words
            for i, v in enumerate(node):
                if isinstance(v, str):
                    if parent_key and parent_key in extra_verbatim:
                        continue
                    node[i] = _scrub_str(v, max_words=max_w)
                else:
                    _walk(v, parent_key=parent_key)

    _walk(obj)
