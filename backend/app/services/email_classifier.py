"""Classify an email as external-customer vs. internal, and tag its bucket.

This is the guardrail that keeps internal chatter — HR, engineering,
finance, all-hands, vendor invoices, recruiter spam — out of the
analysis pipeline.  We run two checks in sequence:

1.  **Deterministic pre-filter** (domain, headers, list-id, auto-gen).
    Cheap, 100% reliable on the easy cases, and used to short-circuit
    before we spend a Haiku token.
2.  **Haiku classifier** with a strict system prompt that demands a JSON
    verdict *and* a confidence score.  If the prefilter is uncertain, we
    fall through to this.  Anything below a configurable confidence
    threshold is treated as internal and dropped — we favour false
    negatives (missed customer email) over false positives (pulling
    internal conversations into client-facing dashboards).

The classifier also assigns a coarse bucket — sales / support / it /
other — so downstream routing and scorecards can branch on it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import anthropic

from backend.app.config import get_settings
from backend.app.services.triage_service import _strip_json_fences

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Anything at or above this confidence gets accepted as external.
EXTERNAL_CONFIDENCE_THRESHOLD = 0.8

SYSTEM_PROMPT = (
    "You are a strict email triage classifier for a B2B conversation "
    "intelligence platform. Decide whether a given email is part of a "
    "CUSTOMER-FACING conversation (sales, customer service, or IT/technical "
    "support with an EXTERNAL customer) or an INTERNAL company email that "
    "must be excluded from analysis.\n\n"
    "Treat the following as INTERNAL (is_external=false):\n"
    "- Any email where both sender and all recipients are on the tenant's "
    "own domain(s)\n"
    "- HR, payroll, benefits, recruiting, legal, finance, procurement, "
    "engineering, IT-help-desk-for-employees\n"
    "- All-hands announcements, internal newsletters, status reports\n"
    "- Calendar invites to internal meetings\n"
    "- Automated notifications from internal tooling (CI, monitoring, "
    "git, Jira, Slack digests, password reset)\n"
    "- Vendor invoices, shipping notifications, marketing blasts the "
    "company receives\n"
    "- Personal email\n\n"
    "Treat the following as EXTERNAL (is_external=true):\n"
    "- A prospect, lead, or existing customer reaching out\n"
    "- Customer support / technical support requests from external users\n"
    "- Replies between a company agent and a customer on the same thread\n"
    "- Sales conversations with prospects\n\n"
    "Return ONLY a JSON object (no markdown fences) with exactly:\n"
    "- is_external: bool\n"
    "- confidence: float 0.0-1.0\n"
    "- classification: 'sales' | 'support' | 'it' | 'other' (only meaningful "
    "when is_external is true; use 'other' otherwise)\n"
    "- reason: short string explaining the decision\n\n"
    "Be conservative. When in doubt, return is_external=false. Never "
    "classify company-internal communication as external."
)


@dataclass
class EmailForClassification:
    subject: Optional[str]
    from_address: str
    to_addresses: Sequence[str]
    cc_addresses: Sequence[str]
    body_preview: str
    headers: Dict[str, str]
    tenant_domains: Sequence[str]  # domains considered "internal"


@dataclass
class ClassificationResult:
    is_external: bool
    confidence: float
    classification: str  # sales|support|it|other
    reason: str


# ── Deterministic pre-filter ───────────────────────────────


_AUTO_HEADER_KEYS = {
    "list-id",
    "list-unsubscribe",
    "auto-submitted",
    "precedence",  # bulk / list / junk
    "x-auto-response-suppress",
}


def _email_domain(addr: str) -> str:
    addr = (addr or "").lower()
    if "<" in addr and ">" in addr:
        addr = addr.split("<", 1)[1].split(">", 1)[0]
    return addr.split("@")[-1].strip() if "@" in addr else ""


def _prefilter(email: EmailForClassification) -> Optional[ClassificationResult]:
    """Return a fast decision if possible, else None to fall through to LLM."""
    # Mailing lists / automated messages are always internal-to-us noise.
    for k in _AUTO_HEADER_KEYS:
        if k in {h.lower() for h in email.headers.keys()}:
            return ClassificationResult(
                is_external=False,
                confidence=0.99,
                classification="other",
                reason=f"Automated/list header ({k}) — excluded",
            )

    tenant_domains = {d.lower().lstrip("@") for d in email.tenant_domains if d}
    if not tenant_domains:
        return None  # No domains configured — can't pre-decide.

    from_domain = _email_domain(email.from_address)
    recipient_domains = {
        _email_domain(a)
        for a in list(email.to_addresses) + list(email.cc_addresses)
        if a
    }
    recipient_domains.discard("")

    sender_internal = from_domain in tenant_domains
    all_recipients_internal = bool(recipient_domains) and recipient_domains.issubset(
        tenant_domains
    )

    if sender_internal and all_recipients_internal:
        return ClassificationResult(
            is_external=False,
            confidence=0.99,
            classification="other",
            reason="Both sender and all recipients are on tenant domains.",
        )

    # If *any* counterparty is external, we still need the LLM to decide
    # whether it's actually sales/support/IT or e.g. vendor spam.
    return None


# ── Haiku classifier ───────────────────────────────────────


class EmailClassifier:
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=get_settings().ANTHROPIC_API_KEY
        )

    async def classify(self, email: EmailForClassification) -> ClassificationResult:
        pre = _prefilter(email)
        if pre is not None:
            return pre

        user_content = (
            f"## Tenant-internal domains\n"
            f"{', '.join(email.tenant_domains) or '(none configured)'}\n\n"
            f"## Email\n"
            f"From: {email.from_address}\n"
            f"To: {', '.join(email.to_addresses)}\n"
            f"Cc: {', '.join(email.cc_addresses)}\n"
            f"Subject: {email.subject or '(no subject)'}\n"
            f"Headers-of-interest: "
            f"{json.dumps({k: v for k, v in email.headers.items() if k.lower() in {'list-id','auto-submitted','precedence','x-mailer','return-path'}})}\n\n"
            f"Body preview:\n{email.body_preview[:2000]}"
        )

        try:
            response = await self._client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=256,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = response.content[0].text
            data: Dict[str, Any] = json.loads(_strip_json_fences(raw))
            is_external = bool(data.get("is_external", False))
            confidence = float(data.get("confidence", 0.0))
            classification = str(data.get("classification", "other")).lower()
            if classification not in {"sales", "support", "it", "other"}:
                classification = "other"
            reason = str(data.get("reason", ""))

            # Confidence gate — err toward internal.
            if is_external and confidence < EXTERNAL_CONFIDENCE_THRESHOLD:
                return ClassificationResult(
                    is_external=False,
                    confidence=confidence,
                    classification="other",
                    reason=f"Low-confidence external verdict ({confidence:.2f}); excluded",
                )

            return ClassificationResult(
                is_external=is_external,
                confidence=confidence,
                classification=classification if is_external else "other",
                reason=reason,
            )
        except (anthropic.APIError, json.JSONDecodeError, ValueError) as exc:
            logger.exception("Email classification failed; defaulting to internal")
            return ClassificationResult(
                is_external=False,
                confidence=0.0,
                classification="other",
                reason=f"Classifier error ({exc.__class__.__name__}); excluded as a safe default",
            )
