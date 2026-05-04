"""Entity resolution — extract Customer + Contacts from a freshly-analyzed
interaction and link the row to canonical records.

The pipeline already produces a rich ``insights`` dict (sentiment, topics,
action items, etc.) but never names the *customer* (the org being sold to)
or the *contacts* (people on the call) in a structured way. This module
fills that gap with a single focused Haiku pass over the compressed
transcript + analysis JSON, then routes the candidates through a
confidence-tier fuser:

* **>=0.80**  — auto-link or auto-create. Pipeline writes the FK on the
  interaction. Audit-log entry posted.
* **0.60–0.80** — surface as a suggestion (notification tray + inline
  card on the orphan interaction). Interaction stays orphan.
* **<0.60** — nothing happens automatically; the user creates the
  customer manually.

Multi-source signal fusion (see :func:`_score_candidates`) widens the
candidate pool beyond the tenant's existing customers to include
CRM-synced organisations from connected HubSpot / Salesforce / Pipedrive
adapters. Google / MS / Zoho / MS Dynamics are explicitly noted in the
plan but not yet wired through the CRM layer; ``crm_signal_fusion`` is
the slot they'll plug into when those adapters land.

The ``role`` / ``role_confidence`` columns on Contact are populated here
too with the same banding (champion / economic_buyer / user / blocker /
coach), per the plan.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from rapidfuzz import fuzz, process
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.models import Contact, Customer, CustomerOwner, Interaction
from backend.app.services.llm_client import get_async_anthropic

logger = logging.getLogger(__name__)


# Confidence band thresholds. The 0.80 / 0.60 boundaries are the same
# ones surfaced in the plan and the SPA chip styling — keep them
# centralised so backend / frontend can never drift.
AUTO_THRESHOLD = 0.80
SUGGEST_THRESHOLD = 0.60

# Lower threshold specifically for *contact roles*. Roles are softer
# signals than customer identity — a clear "champion" hint in dialogue
# rarely lands above 0.6 from Haiku because the model hedges. The
# 2026-05-03 backfill produced 12 contacts, all with NULL roles,
# because every confidence sat in the 0.4–0.55 range. A 0.45 floor
# surfaces the role with a "suggested" visual treatment in the SPA
# (dashed border + hover prompt) while still rejecting outright
# guesses below 0.45.
ROLE_SUGGEST_THRESHOLD = 0.45

# A name that's actually a role title rather than a person. The
# 2026-05-03 backfill created Contact rows for "CFO" and "IT Director"
# because the LLM listed them when the speaker mentioned them by
# title — even though no proper name was used and they didn't
# actually participate. Anything matching this set (case-insensitive,
# whole-string after strip) is rejected before a Contact row is
# created. The prompt also tells the LLM to skip these now, but the
# server-side guard makes the fix two-layered.
_ROLE_TITLE_REJECT = frozenset(
    s.lower()
    for s in (
        "ceo", "cfo", "cto", "coo", "cmo", "cro", "ciso", "cpo", "chro",
        "vp", "svp", "evp", "avp",
        "vp engineering", "vp sales", "vp ops", "vp marketing", "vp product",
        "director", "manager", "lead",
        "it director", "engineering director", "sales director",
        "legal team", "legal", "compliance", "finance team", "finance",
        "the cfo", "the cto", "the ceo", "the legal team",
        "rep", "agent", "csm", "ae", "sdr", "sales rep",
        "customer", "prospect", "buyer",
    )
)

# Buying-group role vocabulary mirrored from the DB CHECK constraint
# (``ck_contacts_role``). The LLM is instructed to pick from this list
# only; anything outside is dropped.
VALID_CONTACT_ROLES = frozenset(
    {"champion", "economic_buyer", "user", "blocker", "coach"}
)


# ── Extraction prompt (Haiku) ────────────────────────────────────────────

_EXTRACTION_SYSTEM_PROMPT = (
    "You are Linda, the AI listening to this call. You are doing one "
    "narrow task: identify the *customer organization* that the rep is "
    "selling to or supporting, and the *people* who participated. Speak "
    "in the first person where prose is required, but the response must "
    "be valid JSON only.\n\n"
    "Return JSON with these exact keys:\n\n"
    "{\n"
    '  "customer": {\n'
    '    "name": str | null,\n'
    '    "name_confidence": float 0.0-1.0,\n'
    '    "domain_hint": str | null,\n'
    '    "domain_confidence": float 0.0-1.0,\n'
    '    "evidence_excerpt": str | null\n'
    "  },\n"
    '  "contacts": [\n'
    "    {\n"
    '      "name": str,\n'
    '      "title": str | null,\n'
    '      "side": "rep" | "customer" | "unknown",\n'
    '      "role": "champion" | "economic_buyer" | "user" | "blocker" | "coach" | null,\n'
    '      "role_confidence": float 0.0-1.0,\n'
    '      "evidence_excerpt": str | null\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Hard rules:\n"
    "- The customer is the organisation the rep is selling to / serving. "
    "  If the rep's own org is also mentioned, do NOT return it as the "
    "  customer.\n"
    "- ``name_confidence`` reflects how unambiguously the transcript "
    "  identifies the org. A clear repeated mention with a title like "
    "  'VP at Riverbank Manufacturing' = 0.9+. A passing reference once "
    "  = 0.4. No identifiable customer = null name with confidence 0.0.\n"
    "- ``domain_hint`` is whatever URL / email-domain you can extract "
    "  from the transcript (or null). Do not invent domains; do not "
    "  guess based on the company name.\n"
    "- For each contact, ``role`` must be exactly one of the five "
    "  values listed (or null if you can't tell). Use null rather than "
    "  inventing labels like 'decision_maker' or 'sponsor'.\n"
    "- ``role_confidence`` is your certainty about the role assignment. "
    "  A contact who explicitly says 'I'll be the budget owner' = 0.9 "
    "  for economic_buyer. A contact who is supportive but quiet = 0.4 "
    "  champion. Be honest about uncertainty.\n"
    "- Skip people who are only mentioned by name without participating "
    "  (e.g. 'I'll loop in Brendan' — Brendan is not a contact unless "
    "  he speaks). Mentioned-but-not-present people belong elsewhere.\n"
    "- Skip people referred to ONLY by role/title with no proper name "
    "  ('my CFO', 'our IT director', 'the legal team'). A contact "
    "  needs an actual personal name like 'David Aluko' or 'Maria'; "
    "  if the speaker mentions 'my CFO' but never names them, do NOT "
    "  return them.\n"
    "- If the call has no identifiable customer (purely internal call, "
    "  spam, etc.) return ``customer.name = null`` and an empty contacts "
    "  list. Do not guess.\n\n"
    "Return only the JSON. No markdown fences, no preamble."
)


@dataclass
class CustomerCandidate:
    """One candidate match for the call's customer.

    Comes from the LLM extraction, the tenant's existing ``customers``
    rows, and the connected-CRM-sync candidate pool. ``score`` is the
    final fused confidence in [0, 1]; ``source`` describes where the
    candidate came from.
    """

    name: str
    domain: Optional[str] = None
    customer_id: Optional[uuid.UUID] = None
    crm_id: Optional[str] = None
    crm_source: Optional[str] = None
    score: float = 0.0
    source: str = "extracted"  # extracted | existing | crm_sync


@dataclass
class ContactExtraction:
    """One person identified on the call."""

    name: str
    title: Optional[str] = None
    side: str = "unknown"  # rep | customer | unknown
    role: Optional[str] = None
    role_confidence: float = 0.0
    evidence_excerpt: Optional[str] = None


@dataclass
class ExtractionResult:
    customer_name: Optional[str] = None
    customer_name_confidence: float = 0.0
    customer_domain_hint: Optional[str] = None
    customer_domain_confidence: float = 0.0
    customer_evidence: Optional[str] = None
    contacts: List[ContactExtraction] = field(default_factory=list)


@dataclass
class ResolutionOutcome:
    """What entity_resolution did to the interaction.

    Returned to the caller (``_run_pipeline_impl``) so it can decide
    what to surface in the UI / audit log.
    """

    customer_id: Optional[uuid.UUID] = None
    customer_score: float = 0.0
    customer_action: str = "none"  # auto_linked | auto_created | suggested | none
    contact_ids: List[uuid.UUID] = field(default_factory=list)
    suggestions: List[Dict[str, Any]] = field(default_factory=list)


# ── Public entry point ──────────────────────────────────────────────────


async def resolve_interaction_entities(
    *,
    session: Session,
    interaction: Interaction,
    tenant: Any,
    insights: Dict[str, Any],
    compressed_transcript: str,
) -> ResolutionOutcome:
    """Run end-to-end entity resolution for one interaction.

    Called from ``_run_pipeline_impl`` after analysis completes and
    *before* ``status='analyzed'`` is set. Mutates ``interaction`` in
    place (sets ``customer_id`` and ``contact_id``) and adds new
    ``Customer`` / ``Contact`` / ``CustomerOwner`` rows where warranted.
    Does NOT commit — the caller's existing ``session.commit()`` flushes
    the lot.
    """
    own_org = (getattr(tenant, "tenant_context", None) or {}).get("own_org_name")

    # Debug trace stashed on interaction.insights so we can read decisions
    # back via the existing GET /interactions/{id} surface without
    # standing up new endpoints. Two bugs survived the Phase 3B verify
    # pass — contact roles always NULL and Acme dupes despite the
    # advisory lock — so this PR makes every entity_resolution decision
    # observable from the row itself. Cheap to keep around long-term:
    # one dict per analyzed interaction, useful for "why did Linda
    # decide this?" support questions.
    debug: Dict[str, Any] = {
        "own_org_name": own_org,
        "lock_acquired": False,
        "lock_error": None,
    }

    extraction = await _extract(
        insights=insights,
        compressed_transcript=compressed_transcript,
        own_org_name=own_org,
    )
    debug["extraction"] = {
        "customer_name": extraction.customer_name,
        "customer_name_confidence": extraction.customer_name_confidence,
        "customer_domain_hint": extraction.customer_domain_hint,
        "contact_count": len(extraction.contacts),
        "contacts": [
            {
                "name": c.name,
                "side": c.side,
                "role": c.role,
                "role_confidence": c.role_confidence,
                "title": c.title,
            }
            for c in extraction.contacts
        ],
    }

    outcome = ResolutionOutcome()

    # ── Customer side ───────────────────────────────────────────────
    if extraction.customer_name:
        # Serialize concurrent customer-create attempts for the same
        # (tenant, normalized name). pg_advisory_xact_lock holds for the
        # duration of the transaction; a second pipeline running the
        # same call (or a different call about the same customer at the
        # same time) waits here until the first commits, then re-queries
        # candidates and finds the freshly-committed row. Without this
        # lock, two concurrent tasks both observed "no Acme exists" and
        # each created a duplicate Acme Logistics row in the 2026-05-03
        # backfill. The hashtext() narrows the lock space to one int4
        # per (tenant, name) pair so the lock table stays bounded.
        lock_key = f"er:{tenant.id}:{extraction.customer_name.lower().strip()}"
        try:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                {"k": lock_key},
            )
            debug["lock_acquired"] = True
            debug["lock_key"] = lock_key
        except Exception as exc:
            # SQLite (used in tests) doesn't have advisory locks.
            # Tests run single-threaded so the dedupe race doesn't apply
            # there; just continue without the lock.
            debug["lock_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.debug(
                "entity_resolution: advisory lock unavailable; continuing"
            )

        candidates = _score_candidates(
            session=session,
            tenant_id=tenant.id,
            extracted_name=extraction.customer_name,
            extracted_domain=extraction.customer_domain_hint,
            extracted_confidence=extraction.customer_name_confidence,
            own_org_name=own_org,
        )
        debug["candidates_count"] = len(candidates)
        debug["top_candidates"] = [
            {
                "name": c.name,
                "score": round(c.score, 3),
                "source": c.source,
                "customer_id": str(c.customer_id) if c.customer_id else None,
                "domain": c.domain,
            }
            for c in candidates[:5]
        ]
        best = candidates[0] if candidates else None
        debug["auto_threshold"] = AUTO_THRESHOLD
        debug["suggest_threshold"] = SUGGEST_THRESHOLD
        debug["best_score"] = round(best.score, 3) if best else None
        if best and best.score >= AUTO_THRESHOLD:
            customer_id = _link_or_create_customer(
                session=session,
                tenant_id=tenant.id,
                candidate=best,
                extracted_domain=extraction.customer_domain_hint,
            )
            interaction.customer_id = customer_id
            outcome.customer_id = customer_id
            outcome.customer_score = best.score
            outcome.customer_action = (
                "auto_linked" if best.customer_id else "auto_created"
            )
            debug["customer_action"] = outcome.customer_action
            debug["customer_id"] = str(customer_id)
            _ensure_owner(
                session=session,
                tenant_id=tenant.id,
                customer_id=customer_id,
                interaction=interaction,
            )
        elif best and best.score >= SUGGEST_THRESHOLD:
            outcome.customer_action = "suggested"
            outcome.customer_score = best.score
            debug["customer_action"] = "suggested"
            outcome.suggestions.append(
                {
                    "kind": "customer_match",
                    "candidates": [
                        {
                            "name": c.name,
                            "domain": c.domain,
                            "customer_id": str(c.customer_id) if c.customer_id else None,
                            "score": round(c.score, 3),
                            "source": c.source,
                        }
                        for c in candidates[:5]
                    ],
                    "extracted_name": extraction.customer_name,
                    "extracted_evidence": extraction.customer_evidence,
                }
            )

    # ── Contacts side ───────────────────────────────────────────────
    # Skip rep-side contacts (those map to Linda Users, not Contact rows).
    # Skip the unknown-side too — too noisy. Customer-side only.
    contact_ids: List[uuid.UUID] = []
    contact_traces: List[Dict[str, Any]] = []
    for c in extraction.contacts:
        if c.side != "customer":
            contact_traces.append(
                {"name": c.name, "skipped": f"side={c.side}"}
            )
            continue
        # Capture per-contact resolution trace so we can see exactly
        # what _resolve_contact + _apply_role did. The role-NULL bug
        # in Phase 3B made this trace mandatory: extraction returned
        # role=champion at conf=0.75 but the persisted contact row
        # had role=NULL — we couldn't distinguish "_apply_role wasn't
        # called" from "_apply_role was called but the write was
        # lost" without this surface.
        trace: Dict[str, Any] = {"name": c.name, "role_in": c.role, "role_conf_in": c.role_confidence}
        contact_id = _resolve_contact(
            session=session,
            tenant_id=tenant.id,
            customer_id=outcome.customer_id,
            extraction=c,
            trace=trace,
        )
        trace["resolved_id"] = str(contact_id) if contact_id else None
        contact_traces.append(trace)
        if contact_id is not None:
            contact_ids.append(contact_id)

    outcome.contact_ids = contact_ids
    if contact_ids and interaction.contact_id is None:
        # Link the interaction to the highest-confidence customer-side
        # contact when no contact was previously attached. This is a
        # convenience heuristic — a multi-party call still has all the
        # extracted contacts in ``contact_ids`` for downstream surfaces.
        interaction.contact_id = contact_ids[0]

    # Stash the full debug dict on the interaction's insights so we can
    # diagnose entity-resolution decisions from the existing
    # ``GET /interactions/{id}`` endpoint without standing up a
    # separate observability surface. The pipeline merges this back
    # into the final committed insights — see _run_pipeline_impl.
    debug["resolved_contact_count"] = len(contact_ids)
    debug["resolved_customer_id"] = (
        str(outcome.customer_id) if outcome.customer_id else None
    )
    debug["contact_traces"] = contact_traces
    existing_meta = getattr(interaction, "insights", None) or {}
    if isinstance(existing_meta, dict):
        existing_meta = dict(existing_meta)
    else:
        existing_meta = {}
    existing_meta["entity_resolution_debug"] = debug
    interaction.insights = existing_meta

    return outcome


# ── LLM extraction ──────────────────────────────────────────────────────


async def _extract(
    *,
    insights: Dict[str, Any],
    compressed_transcript: str,
    own_org_name: Optional[str],
) -> ExtractionResult:
    """Call Haiku with the focused extraction prompt."""
    client = get_async_anthropic()

    user_block_parts: List[str] = []
    if own_org_name:
        user_block_parts.append(
            f"The rep's own organisation is **{own_org_name}**. Do NOT "
            f"return it as the customer."
        )
    if insights.get("summary"):
        user_block_parts.append(
            f"My one-paragraph summary of the call:\n{insights['summary']}"
        )
    user_block_parts.append("Compressed transcript:\n" + compressed_transcript[:18_000])
    user_block = "\n\n".join(user_block_parts)

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=_EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_block}],
        )
    except Exception:
        logger.exception("entity_resolution: Haiku call failed")
        return ExtractionResult()

    text = "".join(
        getattr(block, "text", "") for block in (resp.content or [])
    ).strip()
    text = _strip_md_fences(text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(
            "entity_resolution: Haiku returned non-JSON: %s", text[:300]
        )
        return ExtractionResult()

    return _parse_extraction(parsed)


def _strip_md_fences(text: str) -> str:
    """Remove ```json fences if the model emitted them despite instructions."""
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_extraction(payload: Dict[str, Any]) -> ExtractionResult:
    out = ExtractionResult()
    cust = payload.get("customer") or {}
    name = cust.get("name")
    if isinstance(name, str) and name.strip():
        out.customer_name = name.strip()
    out.customer_name_confidence = _coerce_unit_float(cust.get("name_confidence"))
    domain = cust.get("domain_hint")
    if isinstance(domain, str) and domain.strip():
        out.customer_domain_hint = domain.strip().lower()
    out.customer_domain_confidence = _coerce_unit_float(cust.get("domain_confidence"))
    if isinstance(cust.get("evidence_excerpt"), str):
        out.customer_evidence = cust["evidence_excerpt"][:600]

    for raw in payload.get("contacts") or []:
        if not isinstance(raw, dict):
            continue
        nm = raw.get("name")
        if not isinstance(nm, str) or not nm.strip():
            continue
        side = raw.get("side") if raw.get("side") in ("rep", "customer", "unknown") else "unknown"
        role = raw.get("role")
        if role not in VALID_CONTACT_ROLES:
            role = None
        out.contacts.append(
            ContactExtraction(
                name=nm.strip(),
                title=raw.get("title") if isinstance(raw.get("title"), str) else None,
                side=side,
                role=role,
                role_confidence=_coerce_unit_float(raw.get("role_confidence")),
                evidence_excerpt=(
                    raw.get("evidence_excerpt")[:600]
                    if isinstance(raw.get("evidence_excerpt"), str)
                    else None
                ),
            )
        )
    return out


def _coerce_unit_float(value: Any) -> float:
    """Clamp arbitrary input to [0.0, 1.0] for confidence fields."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


# ── Multi-source candidate scoring ──────────────────────────────────────


def _score_candidates(
    *,
    session: Session,
    tenant_id: uuid.UUID,
    extracted_name: str,
    extracted_domain: Optional[str],
    extracted_confidence: float,
    own_org_name: Optional[str],
) -> List[CustomerCandidate]:
    """Return candidates ranked by fused confidence, descending.

    Scoring blends the LLM's confidence in the extracted name with the
    fuzzy match score against every candidate from:

    - the tenant's existing ``customers`` rows (the only data source that
      can yield ``customer_id`` for an auto-link),
    - CRM-synced candidates from ``crm_signal_fusion`` (HubSpot,
      Salesforce, Pipedrive today; more providers later).

    The LLM's own ``extracted_name`` is *also* a candidate — that's how
    a brand-new customer gets created when no existing row matches.
    """
    candidates: List[CustomerCandidate] = []

    # Existing tenant customers
    existing = session.query(Customer).filter(Customer.tenant_id == tenant_id).all()
    for cust in existing:
        if own_org_name and _is_same_org(cust.name, own_org_name):
            continue
        candidates.append(
            CustomerCandidate(
                name=cust.name,
                domain=cust.domain,
                customer_id=cust.id,
                crm_id=cust.crm_id,
                source="existing",
            )
        )

    # CRM-synced candidates (separate module so adapters can land
    # without touching this file).
    from backend.app.services.crm_signal_fusion import gather_crm_candidates

    candidates.extend(gather_crm_candidates(session=session, tenant_id=tenant_id))

    # The LLM's own extracted name as a "create-new" candidate. Always
    # included so we have a fallback even when nothing matches.
    candidates.append(
        CustomerCandidate(
            name=extracted_name,
            domain=extracted_domain,
            customer_id=None,
            source="extracted",
        )
    )

    for cand in candidates:
        cand.score = _fuse_score(
            extracted_name=extracted_name,
            extracted_domain=extracted_domain,
            extracted_confidence=extracted_confidence,
            candidate_name=cand.name,
            candidate_domain=cand.domain,
            is_new=cand.source == "extracted",
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _fuse_score(
    *,
    extracted_name: str,
    extracted_domain: Optional[str],
    extracted_confidence: float,
    candidate_name: str,
    candidate_domain: Optional[str],
    is_new: bool,
) -> float:
    """Blend name similarity, domain match, and LLM confidence into [0,1].

    The weights here were chosen so that:
    - A perfect name match (>=95) with a confident extraction (>=0.8)
      and matching domain → ~0.99 (auto-link)
    - A perfect name match without domain corroboration on a confident
      extraction → ~0.99 (auto-link, beats the 'create new' candidate)
    - A typo'd name (token_set 70) with a confident extraction → ~0.77
      (suggest, not auto)
    - The "create-new" candidate → ``extracted_confidence * 0.85``,
      slightly discounted from the LLM's raw confidence so existing
      perfect-name matches always beat creating a duplicate when no
      domain is available to corroborate either side. Without this
      discount, the 2026-05-04 dedupe diagnostic showed a perfect-
      match existing Acme row scoring 0.887 against the extracted
      candidate's 0.95 — the LLM's confidence was beating the
      already-known customer. The discount makes the ranking match
      the obvious user expectation: link before create.
    """
    if is_new:
        # Tiny discount on raw LLM confidence so we don't auto-create
        # a duplicate when an existing row matches well.
        return extracted_confidence * 0.85

    # rapidfuzz returns 0–100; normalise to 0–1.
    token_set = fuzz.token_set_ratio(extracted_name, candidate_name) / 100.0
    partial = fuzz.partial_ratio(extracted_name, candidate_name) / 100.0
    name_sim = max(token_set, partial * 0.95)  # token_set is the trusted signal

    domain_boost = 0.0
    if extracted_domain and candidate_domain:
        if extracted_domain == candidate_domain:
            domain_boost = 0.15
        elif _domain_root(extracted_domain) == _domain_root(candidate_domain):
            domain_boost = 0.10

    # An "existing" candidate (already in our DB) gets a small bonus
    # reflecting the fact that we've seen this customer before. The
    # bonus is large enough to win the tie against the discounted
    # "create new" candidate when names match exactly without a
    # corroborating domain, but small enough that a clearly-different
    # existing row can still lose to a confident new extraction.
    existing_bonus = 0.10

    # 65% name similarity, 25% LLM confidence, +bonus + domain boost.
    fused = (name_sim * 0.65) + (extracted_confidence * 0.25) + domain_boost + existing_bonus
    return min(fused, 1.0)


def _domain_root(domain: str) -> str:
    """Strip subdomains for fuzzy domain matching: 'sales.acme.com' → 'acme.com'."""
    parts = domain.lower().strip().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain.lower()


def _is_same_org(name_a: str, name_b: str) -> bool:
    """Return True when two org strings look like the same company."""
    return fuzz.token_set_ratio(name_a, name_b) >= 90


# ── Customer link / create + ownership ──────────────────────────────────


def _link_or_create_customer(
    *,
    session: Session,
    tenant_id: uuid.UUID,
    candidate: CustomerCandidate,
    extracted_domain: Optional[str],
) -> uuid.UUID:
    """Reuse an existing customer or insert a new one. Returns the FK."""
    if candidate.customer_id is not None:
        # Backfill domain on the existing row when the LLM found one and
        # we didn't have it. Cheap quality boost on every analyzed call.
        if extracted_domain:
            existing = session.get(Customer, candidate.customer_id)
            if existing and not existing.domain:
                existing.domain = extracted_domain
        return candidate.customer_id

    cust = Customer(
        tenant_id=tenant_id,
        name=candidate.name,
        domain=extracted_domain or candidate.domain,
    )
    session.add(cust)
    session.flush()
    logger.info(
        "entity_resolution: created Customer name=%s id=%s tenant=%s",
        cust.name, cust.id, tenant_id,
    )
    return cust.id


def _ensure_owner(
    *,
    session: Session,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    interaction: Interaction,
) -> None:
    """Add the call's rep (Interaction.agent_id) to the customer's owners.

    First touch becomes 'primary'; subsequent reps become 'secondary'.
    Idempotent — re-running over the same (customer, user) pair is a
    no-op thanks to the unique constraint on the join table.
    """
    user_id = interaction.agent_id
    if user_id is None:
        return

    has_primary = (
        session.query(CustomerOwner)
        .filter(
            CustomerOwner.customer_id == customer_id,
            CustomerOwner.role == "primary",
        )
        .first()
    )
    role = "secondary" if has_primary else "primary"

    existing = (
        session.query(CustomerOwner)
        .filter(
            CustomerOwner.customer_id == customer_id,
            CustomerOwner.user_id == user_id,
        )
        .first()
    )
    if existing is not None:
        return  # Already an owner; idempotent.

    session.add(
        CustomerOwner(
            tenant_id=tenant_id,
            customer_id=customer_id,
            user_id=user_id,
            role=role,
            assigned_via="speaker_tag" if interaction.agent_id else "first_uploader",
        )
    )


# ── Contact resolution ──────────────────────────────────────────────────


def _resolve_contact(
    *,
    session: Session,
    tenant_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    extraction: ContactExtraction,
    trace: Optional[Dict[str, Any]] = None,
) -> Optional[uuid.UUID]:
    """Find or create a Contact for one extracted person.

    Today's logic is conservative: prefer existing Contact rows scoped to
    the customer (when known), then to the tenant. A new Contact is
    created when the LLM is sufficiently confident in the name AND we
    have a customer to attach to. Mentions without a resolved customer
    don't get promoted — they'll stay in the interaction's transcript
    and the customer page's "mentions" view (built later).
    """
    raw_name = extraction.name.strip()
    if not raw_name:
        if trace is not None:
            trace["path"] = "rejected_empty_name"
        return None

    # Reject role-title-only entries. The LLM is told to skip these in
    # the prompt; this is the belt-and-braces validator that catches
    # any that slipped through. Anything matching the title set is
    # treated as a mention, not a contact.
    if raw_name.lower() in _ROLE_TITLE_REJECT:
        if trace is not None:
            trace["path"] = "rejected_role_title"
        logger.info(
            "entity_resolution: rejecting role-title-as-name '%s' (no proper name)",
            raw_name,
        )
        return None

    # Heuristic: a real personal name has at least one letter and
    # isn't a single short token that's all-lowercase / matches
    # generic words. "Andrei" is valid. "the team" is not. "Maria"
    # is valid. "buyer" is not (caught above). This catches odd
    # outputs like "Champion" that don't match the explicit reject
    # list but obviously aren't names.
    if not _looks_like_personal_name(raw_name):
        if trace is not None:
            trace["path"] = "rejected_non_name"
        logger.info(
            "entity_resolution: rejecting non-name '%s'", raw_name
        )
        return None

    base_query = session.query(Contact).filter(Contact.tenant_id == tenant_id)
    if customer_id is not None:
        base_query = base_query.filter(Contact.customer_id == customer_id)

    pool = base_query.all()
    best_match: Optional[Contact] = None
    best_score = 0.0
    for c in pool:
        if not c.name:
            continue
        s = fuzz.token_set_ratio(extraction.name, c.name) / 100.0
        if s > best_score:
            best_match = c
            best_score = s

    if best_match is not None and best_score >= AUTO_THRESHOLD:
        if trace is not None:
            trace["path"] = "match_existing"
            trace["match_score"] = round(best_score, 3)
            trace["role_before"] = best_match.role
            trace["role_conf_before"] = best_match.role_confidence
        _apply_role(best_match, extraction)
        if trace is not None:
            trace["role_after"] = best_match.role
            trace["role_conf_after"] = best_match.role_confidence
        return best_match.id

    # Create a new Contact only when we have a customer to attach to —
    # orphan contacts pollute the table and rarely earn their keep.
    if customer_id is None:
        if trace is not None:
            trace["path"] = "skipped_no_customer"
        return None

    contact = Contact(
        tenant_id=tenant_id,
        customer_id=customer_id,
        name=extraction.name,
    )
    _apply_role(contact, extraction)
    if trace is not None:
        trace["path"] = "create_new"
        trace["role_after"] = contact.role
        trace["role_conf_after"] = contact.role_confidence
    session.add(contact)
    session.flush()
    return contact.id


def _apply_role(contact: Contact, extraction: ContactExtraction) -> None:
    """Set role + role_confidence on the contact when the LLM is confident
    enough.

    Roles use ``ROLE_SUGGEST_THRESHOLD`` (0.45), which is lower than the
    customer/contact-creation suggest band (0.60). Reason: Haiku
    routinely returns 0.45–0.55 confidence on clear role signals
    because it hedges, and the SPA renders sub-0.6 roles as a
    "suggested" chip (dashed border + hover prompt to confirm) — that
    UX is exactly the right surface for this confidence band, so we
    accept and let the user decide.

    Below 0.45 the role stays untouched. We never *clear* an existing
    role here — the user's manual edits should survive a noisy
    follow-up call where the LLM was less sure.
    """
    if extraction.role and extraction.role_confidence >= ROLE_SUGGEST_THRESHOLD:
        # Only overwrite if the new signal beats the stored one (or
        # there's no stored one yet).
        existing_conf = contact.role_confidence or 0.0
        if extraction.role_confidence >= existing_conf:
            contact.role = extraction.role
            contact.role_confidence = extraction.role_confidence


def _looks_like_personal_name(s: str) -> bool:
    """Heuristic: does this look like a real person's name?

    Trues: "Maria", "David Aluko", "Allison Park", "Andrei", "Jean-Luc".
    Falses: "the team", "buyer", "Champion", "senior dispatcher",
    "engineering manager", short single-word common-noun strings.

    Rule: a real name has at least one *capitalised* word. Proper
    names follow this convention almost universally — if every word
    in a string is lowercase ("senior dispatcher", "the legal team"),
    it's a job description, not a person.

    The 2026-05-04 backfill surfaced "senior dispatcher" as a Contact
    row — the LLM emitted it from a transcript line where the rep
    confirmed "senior dispatcher involvement in Thursday meeting".
    The previous heuristic accepted it because multi-word strings
    bypassed the lowercase guard. Tightened to require at least one
    capitalized word.

    Permissive about hyphens ("Jean-Luc" splits to ["Jean", "Luc"],
    both capitalised) and single capitalised names ("Andrei" passes).
    """
    t = s.strip()
    if len(t) < 2:
        return False
    if not any(ch.isalpha() for ch in t):
        return False
    if t.lower().startswith(("the ", "a ", "an ")):
        return False
    # At least one word must start with an uppercase letter. Splitting
    # on whitespace AND hyphens covers "Jean-Luc" properly.
    words = re.split(r"[\s\-]+", t)
    has_capitalised_word = any(w and w[0].isupper() for w in words)
    return has_capitalised_word
