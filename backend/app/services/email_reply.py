"""Draft an email reply using all available context.

Context sources assembled into the Sonnet prompt:

1.  **Global knowledge base** (``kb_documents``) — product docs, playbooks,
    approved language the tenant wants on brand.
2.  **Contact history** — all prior Interactions with this contact
    (voice + email + chat), their AI-generated summaries and sentiment
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

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import (
    Contact,
    Conversation,
    Interaction,
    KBDocument,
    Tenant,
)
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
    "- Acknowledges any relevant history from prior conversations — "
    "names, prior issues, preferences — to show continuity.\n"
    "- Matches the tenant's tone.\n"
    "- Avoids internal jargon and speculative commitments.\n\n"
    "Return ONLY valid JSON (no markdown) with these fields:\n"
    "- subject: string (include 'Re:' if replying)\n"
    "- body: string (plain text, greeting → body → signoff)\n"
    "- rationale: string (1-3 sentences — why you chose this response)\n"
    "- citations: list of {source: str, snippet: str} — which KB docs or "
    "prior interactions you drew from; empty list if none\n"
    "- requires_human_review: bool — true if the reply makes a "
    "commitment that should be checked (pricing, deadlines, legal)\n"
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


async def _conversation_messages(db: AsyncSession, conversation_id) -> List[Interaction]:
    result = await db.execute(
        select(Interaction)
        .where(Interaction.conversation_id == conversation_id)
        .order_by(Interaction.created_at.asc())
    )
    return result.scalars().all()


async def _contact_history(
    db: AsyncSession, tenant_id, contact_id, exclude_conversation_id, limit: int = 10
) -> List[Interaction]:
    if contact_id is None:
        return []
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
    return result.scalars().all()


async def _kb_excerpts(db: AsyncSession, tenant_id, query: str, k: int = 5) -> List[KBDocument]:
    """Pull grounding docs via the retrieval service.

    Uses Qdrant + embeddings when configured, and a tenant-scoped
    keyword ranker otherwise.  See services/kb_retrieval.py.
    """
    from backend.app.services.kb_retrieval import retrieve

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
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=get_settings().ANTHROPIC_API_KEY
        )

    async def draft(
        self,
        db: AsyncSession,
        tenant: Tenant,
        conversation: Conversation,
        extra_instructions: Optional[str] = None,
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

        user_content = (
            f"## Tenant\n"
            f"name: {tenant.name}\n"
            f"tone: {_tenant_tone(tenant)}\n\n"
            f"## Conversation classification\n"
            f"{conversation.classification or 'unknown'}\n\n"
            f"## Contact\n"
            f"name: {contact.name if contact else '(unknown)'}\n"
            f"email: {contact.email if contact else '(unknown)'}\n"
            f"prior interactions: {contact.interaction_count if contact else 0}\n"
            f"sentiment trend: {contact.sentiment_trend if contact else []}\n\n"
            f"## Prior conversations with this contact\n"
            f"{_format_history(history)}\n\n"
            f"## Current thread (most recent message last)\n"
            f"{_format_thread(messages)}\n\n"
            f"## Knowledge base excerpts\n"
            f"{_format_kb(kb_docs)}\n\n"
            f"## Extra instructions from the agent\n"
            f"{extra_instructions or '(none)'}\n"
        )

        response = await self._client.messages.create(
            model=SONNET,
            max_tokens=2048,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
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
