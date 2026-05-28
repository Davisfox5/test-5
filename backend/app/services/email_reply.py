"""Draft an email reply using all available context.

Context sources assembled into the Sonnet prompt:

1.  **Global knowledge base** (``kb_documents``) — product docs, playbooks,
    approved language the tenant wants on brand.
2.  **Contact history** — all prior Interactions with this contact
    (voice + email + transcript), their AI-generated summaries and sentiment
    trajectory.  This is the "client-specific" side.
3.  **Current conversation thread** — the ordered set of messages in
    the active ``Conversation``, including the latest customer message
    we're replying to.
4.  **Tenant tone** — branding config that defines the voice (casual,
    formal, etc.).

The output is a strict JSON document so the caller can render the draft
and surface a rationale without another parse pass.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import time

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services import metrics as _metrics
from backend.app.models import (
    Contact,
    Conversation,
    Interaction,
    KBDocument,
    Tenant,
)
from backend.app.services.llm_client import get_async_anthropic
from backend.app.services.triage_service import _strip_json_fences

logger = logging.getLogger(__name__)

SONNET = "claude-sonnet-4-6"

SYSTEM_PROMPT = (
    "You are drafting a reply email on behalf of a professional "
    "customer-facing agent (sales / customer service / IT support). "
    "You are given (1) the full thread you are replying to, (2) a "
    "history of prior conversations with this contact, (3) relevant "
    "excerpts from the company's knowledge base, and (4) the tenant's "
    "tone of voice.\n\n"
    "Write a reply that:\n"
    "- Directly addresses what the customer asked in the most recent "
    "message.\n"
    "- Uses facts grounded in the knowledge base excerpts when available. "
    "Never fabricate product details, prices, SLAs, or policies.\n"
    "- Acknowledges any relevant history from prior conversations "
    "(names, prior issues, preferences) to show continuity.\n"
    "- Matches the tenant's tone.\n"
    "- Avoids internal jargon and speculative commitments.\n\n"
    "VOICE RULES (read carefully; these are hard requirements)\n"
    "1. Open with a short warm greeting line on its own: 'Hi {Name},' "
    "then ONE friendly opening line ('Great talking with you today.', "
    "'Thanks for the time today.', 'Thanks for the quick reply.'). "
    "Then get into the substance.\n"
    "2. NEVER use all-caps section labels. Bad: '1. CALENDAR:', "
    "'2. AGENDA:', '3. CONTRACT:'. Good: lead each point with a "
    "sentence. If you must enumerate, write '1. I will send three "
    "Thursday options today...' and start the sentence.\n"
    "3. Multiple time options on the SAME date go in one inline "
    "sentence: 'We can do 1:00, 2:30, or 4:00 PM ET on Thursday. "
    "Whichever works best.' Only use a bulleted list when options "
    "span DIFFERENT dates.\n"
    "4. Do NOT over-CYA or stack credentials. One sentence on who "
    "you're bringing and why is enough. Do not pile on 'he has done "
    "your exact stack twice' plus 'two references' plus 'case "
    "studies' unless the customer asked.\n"
    "5. Banned phrases in the email body: 'not after', 'before X, "
    "not after Y', 'I want to make sure', 'I want to ensure', "
    "'Just to be clear', 'One ask from my side', 'One quick ask', "
    "'In an effort to', 'going forward'. Make the request directly; "
    "do not preface the request.\n"
    "6. NEVER use em-dashes (—) or en-dashes (–) in the email body. "
    "Use periods, colons, commas, semicolons, or parentheses instead. "
    "The only exception is verbatim quotes the customer used.\n"
    "7. Closer is warm but professional. Avoid the cute one-word "
    "punch ('Talk Thursday.', 'Onward.', 'Stoked.'). Prefer 'Thanks "
    "again, talk soon.' or 'Looking forward to Thursday. Thanks!' "
    "or a tenant-tone match.\n"
    "8. Sign-off: rep's first name on its own line.\n\n"
    "GOOD body example (Thursday follow-up after a discovery):\n"
    "'Hi David,\n\n"
    "Great talking with you today. Three things to land before "
    "Thursday so we can use the time well.\n\n"
    "1. I'll send three Thursday options today: 1:00, 2:30, and "
    "4:00 PM ET. Let me know which works for you, the CFO, and your "
    "IT lead.\n\n"
    "2. I'm bringing our solutions architect Rajiv to walk through "
    "the TMS integration with your IT team.\n\n"
    "3. I'll have the standard agreement redlined to your legal "
    "team by Wednesday so they can flag anything ahead of the "
    "meeting.\n\n"
    "One quick thing from you: can you share the rubric you and the "
    "CFO are scoring vendors against? I'd rather answer the real "
    "questions on Thursday than guess.\n\n"
    "Thanks again, talk soon.\n"
    "Maria'\n\n"
    "Return ONLY valid JSON (no markdown) with these fields:\n"
    "- subject: string (include 'Re:' if replying)\n"
    "- body: string (plain text following the voice rules above)\n"
    "- rationale: string (1-3 sentences explaining the response "
    "choice; rationale is internal, voice rules above still apply)\n"
    "- citations: list of {source: str, snippet: str} (which KB "
    "docs or prior interactions you drew from; empty list if none)\n"
    "- requires_human_review: bool (true if the reply makes a "
    "commitment that should be checked: pricing, deadlines, legal)\n"
)


@dataclass
class ReplyDraft:
    subject: str
    body: str
    rationale: str
    citations: List[Dict[str, str]]
    requires_human_review: bool


def _tenant_tone(tenant: Tenant) -> str:
    branding = tenant.branding_config or {}
    return branding.get("email_tone") or branding.get("tone") or "professional, concise, warm"


def _tone_examples(tenant: Tenant, classification: Optional[str]) -> str:
    """Return up to 2 tone exemplars matching the conversation classification.

    ``branding_config.email_tone_examples`` is a list of
    ``{"scenario": str, "ideal_response": str, "tags": [str]}`` items.  Tags
    that match the conversation's classification (sales/support/it/other) are
    preferred; otherwise we fall back to the first two regardless.
    """
    branding = tenant.branding_config or {}
    examples = branding.get("email_tone_examples") or []
    if not examples:
        return ""
    target = (classification or "").lower()

    def _matches(ex: Dict[str, Any]) -> bool:
        tags = [str(t).lower() for t in (ex.get("tags") or [])]
        return target in tags if target else False

    matched = [e for e in examples if _matches(e)] or examples
    chosen = matched[:2]
    lines: List[str] = ["## Tone exemplars"]
    for i, ex in enumerate(chosen, start=1):
        scenario = str(ex.get("scenario") or f"example {i}")
        ideal = str(ex.get("ideal_response") or "")
        lines.append(f"### {scenario}\n{ideal[:1500]}")
    return "\n\n".join(lines)


async def _conversation_messages(db: AsyncSession, conversation_id) -> List[Interaction]:
    result = await db.execute(
        select(Interaction)
        .where(Interaction.conversation_id == conversation_id)
        .order_by(Interaction.created_at.asc())
    )
    return result.scalars().all()


_CONTACT_HISTORY_TTL_SECONDS = 120  # 2 min — covers the typical drafting burst
_CONTACT_HISTORY_KEY = "contact:hist:v1:{}:{}"


def _contact_history_redis():
    try:
        import redis  # type: ignore

        from backend.app.config import get_settings

        return redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception:  # pragma: no cover
        return None


def _serialize_history(rows: List[Interaction]) -> str:
    return json.dumps(
        [
            {
                "id": str(r.id),
                "channel": r.channel,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "insights": r.insights or {},
            }
            for r in rows
        ]
    )


def _deserialize_history(blob: str) -> List[Interaction]:
    data = json.loads(blob)
    out: List[Interaction] = []
    for item in data:
        i = Interaction()
        i.channel = item.get("channel")
        i.insights = item.get("insights") or {}
        ts = item.get("created_at")
        if ts:
            try:
                i.created_at = datetime.fromisoformat(ts)
            except ValueError:
                i.created_at = None
        out.append(i)
    return out


async def _contact_history(
    db: AsyncSession, tenant_id, contact_id, exclude_conversation_id, limit: int = 10
) -> List[Interaction]:
    if contact_id is None:
        return []

    cache_key = _CONTACT_HISTORY_KEY.format(contact_id, exclude_conversation_id)
    r = _contact_history_redis()
    if r is not None:
        try:
            blob = r.get(cache_key)
            if blob:
                return _deserialize_history(blob)
        except Exception:  # pragma: no cover — cache miss tolerance
            logger.debug("contact_history cache get failed", exc_info=True)

    result = await db.execute(
        select(Interaction)
        .where(
            Interaction.tenant_id == tenant_id,
            Interaction.contact_id == contact_id,
            Interaction.conversation_id != exclude_conversation_id,
        )
        .order_by(Interaction.created_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()

    if r is not None and rows:
        try:
            r.setex(
                cache_key,
                _CONTACT_HISTORY_TTL_SECONDS,
                _serialize_history(rows),
            )
        except Exception:  # pragma: no cover
            logger.debug("contact_history cache set failed", exc_info=True)
    return rows


async def _kb_excerpts(db: AsyncSession, tenant_id, query: str, k: int = 5) -> List[KBDocument]:
    """Pull grounding docs via the retrieval service.

    Uses Qdrant + embeddings when configured, and a tenant-scoped
    keyword ranker otherwise. See services/kb_document_retrieval.py.
    """
    from backend.app.services.kb_document_retrieval import retrieve

    ranked = await retrieve(db, tenant_id, query, k=k)
    return [doc for doc, _score in ranked]


def _format_thread(messages: List[Interaction]) -> str:
    lines: List[str] = []
    for m in messages:
        who = "CUSTOMER" if m.direction == "inbound" else "AGENT"
        ts = m.created_at.isoformat() if m.created_at else ""
        subj = m.subject or m.title or ""
        lines.append(f"--- [{ts}] {who} — {subj}")
        lines.append((m.raw_text or "")[:4000])
    return "\n".join(lines)


def _format_history(history: List[Interaction]) -> str:
    if not history:
        return "(no prior interactions on file)"
    lines: List[str] = []
    for h in history:
        summary = (h.insights or {}).get("summary", "")
        sentiment = (h.insights or {}).get("sentiment_score")
        lines.append(
            f"- {h.channel} {h.created_at.date() if h.created_at else '?'} "
            f"sentiment={sentiment} — {summary[:200]}"
        )
    return "\n".join(lines)


def _format_kb(docs: List[KBDocument]) -> str:
    if not docs:
        return "(no knowledge base articles available)"
    return "\n\n".join(
        f"### {d.title or 'Untitled'} (id={d.id})\n{(d.content or '')[:2000]}"
        for d in docs
    )


class ReplyDrafter:
    def __init__(
        self, client: Optional[anthropic.AsyncAnthropic] = None
    ) -> None:
        self._client = client or get_async_anthropic()

    async def draft(
        self,
        db: AsyncSession,
        tenant: Tenant,
        conversation: Conversation,
        extra_instructions: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
        tenant_context_block: Optional[str] = None,
    ) -> ReplyDraft:
        messages = await _conversation_messages(db, conversation.id)
        history = await _contact_history(
            db, tenant.id, conversation.contact_id, conversation.id
        )
        contact = None
        if conversation.contact_id is not None:
            contact = (
                await db.execute(
                    select(Contact).where(Contact.id == conversation.contact_id)
                )
            ).scalar_one_or_none()
        kb_query = (conversation.subject or "") + "\n" + (
            messages[-1].raw_text if messages else ""
        )
        kb_docs = await _kb_excerpts(db, tenant.id, kb_query)
        tone_block = _tone_examples(tenant, conversation.classification)
        system_prompt = system_prompt_override or SYSTEM_PROMPT

        sections: List[str] = []
        if tenant_context_block:
            sections.append(tenant_context_block)
        sections.extend([
            f"## Tenant\nname: {tenant.name}\ntone: {_tenant_tone(tenant)}",
            f"## Conversation classification\n{conversation.classification or 'unknown'}",
            (
                f"## Contact\n"
                f"name: {contact.name if contact else '(unknown)'}\n"
                f"email: {contact.email if contact else '(unknown)'}\n"
                f"prior interactions: {contact.interaction_count if contact else 0}\n"
                f"sentiment trend: {contact.sentiment_trend if contact else []}"
            ),
            f"## Prior conversations with this contact\n{_format_history(history)}",
            f"## Current thread (most recent message last)\n{_format_thread(messages)}",
            f"## Knowledge base excerpts\n{_format_kb(kb_docs)}",
        ])
        if tone_block:
            sections.append(tone_block)
        sections.append(
            f"## Extra instructions from the agent\n{extra_instructions or '(none)'}"
        )
        user_content = "\n\n".join(sections)

        t0 = time.perf_counter()
        response = await self._client.messages.create(
            model=SONNET,
            max_tokens=2048,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )
        _metrics.LLM_LATENCY.labels(surface="email_reply", model=SONNET).observe(
            time.perf_counter() - t0
        )
        raw = response.content[0].text
        try:
            data: Dict[str, Any] = json.loads(_strip_json_fences(raw))
        except json.JSONDecodeError:
            logger.exception("Reply drafter JSON parse failed; returning raw text")
            return ReplyDraft(
                subject=f"Re: {conversation.subject or ''}",
                body=raw,
                rationale="(model returned unparseable JSON — body shown verbatim)",
                citations=[],
                requires_human_review=True,
            )

        return ReplyDraft(
            subject=str(data.get("subject") or f"Re: {conversation.subject or ''}"),
            body=str(data.get("body") or ""),
            rationale=str(data.get("rationale") or ""),
            citations=list(data.get("citations") or []),
            requires_human_review=bool(data.get("requires_human_review", False)),
        )
