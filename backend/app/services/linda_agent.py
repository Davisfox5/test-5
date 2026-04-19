"""Linda agent — system prompt, tool definitions, and tool-use dispatch for Ask Linda chat.

Read tools execute immediately. Draft tools create a ``WriteProposal`` row
(pending, expires in 24h) and return a preview payload to the LLM — they do
**not** mutate state. The frontend surfaces the proposal as a Confirm / Edit /
Cancel card; only on Confirm does the confirmation endpoint dispatch to the
real mutator.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    ActionItem,
    Conversation,
    ConversationMessage,
    Interaction,
    InteractionSnippet,
    Tenant,
    User,
    WriteProposal,
)
from backend.app.services.llm_client import get_async_anthropic
from backend.app.services.search_service import SearchService

logger = logging.getLogger(__name__)

LINDA_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048
PROPOSAL_TTL = timedelta(hours=24)

# ── System prompt (static portion — cached) ───────────────────────────────

PERSONA = (
    "You are Linda — Listening Intelligence and Natural Dialogue Assistant. "
    "You are the AI orchestrator behind this product, the same brain that "
    "analyzes calls, drafts coaching notes, and extracts action items. You "
    "speak in the first person, calm and attentive, like a thoughtful "
    "colleague who listened to every call.\n\n"
    "Tone: warm, honest, concise. You're not a mascot and you're not "
    "servile. Be direct when there's something worth saying.\n"
    "Never invent data — if you don't have it, use a tool to look it up, "
    "or say you don't know.\n"
    "When proposing a write (creating an action item, drafting an email, "
    "updating a CRM), describe what you're about to propose *before* "
    "calling the tool, so the user knows what's coming. The tool will "
    "return a proposal id — the user will Confirm or Cancel from the UI. "
    "Never pretend a write has happened until the user confirms."
)

PRODUCT_KNOWLEDGE = (
    "## About the product\n"
    "LINDA is a call intelligence platform for sales and support teams. "
    "Every customer call is transcribed, analyzed, and scored. Core "
    "surfaces the user might ask about:\n"
    "- Interactions: list of calls with sentiment, summary, topics\n"
    "- Action Items: follow-ups extracted from calls, with assignee and "
    "due date\n"
    "- Scorecards: QA rubrics applied to each call (Sales QA, CS QA)\n"
    "- Snippets: short clips of notable moments (pricing pushback, "
    "competitor mention, positive feedback)\n"
    "- Live Coaching: real-time hints during active calls\n"
    "- Integrations: Salesforce, HubSpot, Slack, Gmail, Zoom\n"
    "- Webhooks: outbound events for tenant systems"
)


def build_system_blocks(tenant: Tenant, user: Optional[User]) -> List[Dict[str, Any]]:
    """Build the system prompt as a list of blocks so the static portion can be cached."""
    static_text = f"{PERSONA}\n\n{PRODUCT_KNOWLEDGE}"
    user_line = (
        f"Signed in as: {user.name or user.email} ({user.role})"
        if user is not None
        else "Signed in via API key."
    )
    dynamic_text = (
        f"## Tenant context\n"
        f"- Tenant: {tenant.name} ({tenant.slug})\n"
        f"- {user_line}\n"
    )
    return [
        {
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": dynamic_text},
    ]


# ── Tool definitions (cached — static across turns) ───────────────────────

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_interactions",
        "description": "Full-text search across the tenant's call transcripts. Use this when the user asks about past calls, topics, competitors, or specific phrases.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "channel": {"type": "string", "description": "Optional channel filter (phone, email, chat, zoom, meet)."},
                "date_from": {"type": "string", "description": "Optional ISO date lower bound."},
                "date_to": {"type": "string", "description": "Optional ISO date upper bound."},
                "limit": {"type": "integer", "description": "Max results (default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_action_items",
        "description": "List open action items for the tenant. Filter by status, assignee, or due-before.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status (pending, in_progress, completed)."},
                "assignee_email": {"type": "string", "description": "Filter by assignee email."},
                "due_before": {"type": "string", "description": "ISO date — only items due before this date."},
                "limit": {"type": "integer", "description": "Max results (default 20)."},
            },
        },
    },
    {
        "name": "get_interaction_detail",
        "description": "Fetch full detail for a single interaction: summary, sentiment, snippets, action items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {"type": "string", "description": "Interaction UUID."},
            },
            "required": ["interaction_id"],
        },
    },
    {
        "name": "propose_action_item",
        "description": "Propose creating a new action item. Returns a proposal preview; the user confirms from the UI.",
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {"type": "string", "description": "Related interaction UUID, if any."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assignee_email": {"type": "string"},
                "due_date": {"type": "string", "description": "ISO date."},
                "priority": {"type": "string", "description": "high | medium | low"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "propose_email_draft",
        "description": "Propose a follow-up email draft. Returns a proposal preview with subject and body.",
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "recipients": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["subject", "body", "recipients"],
        },
    },
    {
        "name": "propose_crm_update",
        "description": "Propose a CRM update (Salesforce/HubSpot). Returns a proposal preview with the target object and fields.",
        "input_schema": {
            "type": "object",
            "properties": {
                "interaction_id": {"type": "string"},
                "target": {"type": "string", "description": "Target object, e.g. 'salesforce.Opportunity' or 'hubspot.Deal'."},
                "fields": {"type": "object", "description": "Field-name → new-value map."},
            },
            "required": ["target", "fields"],
        },
    },
]

READ_TOOLS = {"search_interactions", "get_action_items", "get_interaction_detail"}
DRAFT_TOOLS = {"propose_action_item", "propose_email_draft", "propose_crm_update"}

DRAFT_KIND_BY_TOOL = {
    "propose_action_item": "action_item",
    "propose_email_draft": "email_draft",
    "propose_crm_update": "crm_update",
}


# ── Dispatcher ─────────────────────────────────────────────────────────────


@dataclass
class AgentContext:
    db: AsyncSession
    tenant: Tenant
    user: Optional[User]
    conversation_id: uuid.UUID


async def _exec_search_interactions(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    svc = SearchService()
    try:
        results = await svc.search(
            tenant_id=str(ctx.tenant.id),
            query=args["query"],
            channel=args.get("channel"),
            date_from=args.get("date_from"),
            date_to=args.get("date_to"),
            limit=int(args.get("limit", 10)),
        )
    except Exception as exc:
        logger.exception("search_interactions failed")
        return {"error": f"search failed: {exc}"}
    return {"results": results}


async def _exec_get_action_items(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    stmt = select(ActionItem).where(ActionItem.tenant_id == ctx.tenant.id)
    if args.get("status"):
        stmt = stmt.where(ActionItem.status == args["status"])
    if args.get("due_before"):
        stmt = stmt.where(ActionItem.due_date <= date.fromisoformat(args["due_before"]))
    if args.get("assignee_email"):
        assignee_stmt = select(User.id).where(
            User.tenant_id == ctx.tenant.id, User.email == args["assignee_email"]
        )
        assignee_id = (await ctx.db.execute(assignee_stmt)).scalar_one_or_none()
        if assignee_id is not None:
            stmt = stmt.where(ActionItem.assigned_to == assignee_id)
    stmt = stmt.order_by(ActionItem.created_at.desc()).limit(int(args.get("limit", 20)))
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return {
        "action_items": [
            {
                "id": str(r.id),
                "title": r.title,
                "description": r.description,
                "status": r.status,
                "priority": r.priority,
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "interaction_id": str(r.interaction_id),
            }
            for r in rows
        ]
    }


async def _exec_get_interaction_detail(ctx: AgentContext, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        interaction_uuid = uuid.UUID(args["interaction_id"])
    except (KeyError, ValueError):
        return {"error": "invalid interaction_id"}

    interaction = (
        await ctx.db.execute(
            select(Interaction).where(
                Interaction.id == interaction_uuid, Interaction.tenant_id == ctx.tenant.id
            )
        )
    ).scalar_one_or_none()
    if interaction is None:
        return {"error": "interaction not found"}

    snippet_rows = (
        await ctx.db.execute(
            select(InteractionSnippet).where(InteractionSnippet.interaction_id == interaction_uuid)
        )
    ).scalars().all()

    insights = interaction.insights or {}
    return {
        "interaction": {
            "id": str(interaction.id),
            "title": interaction.title,
            "summary": insights.get("summary"),
            "sentiment_overall": insights.get("sentiment_overall"),
            "sentiment_score": insights.get("sentiment_score"),
            "channel": interaction.channel,
            "created_at": interaction.created_at.isoformat() if interaction.created_at else None,
        },
        "snippets": [
            {
                "title": s.title,
                "description": s.description,
                "quality": s.quality,
                "start_time": s.start_time,
                "end_time": s.end_time,
            }
            for s in snippet_rows
        ],
    }


async def _create_proposal(
    ctx: AgentContext, kind: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    proposal = WriteProposal(
        conversation_id=ctx.conversation_id,
        tenant_id=ctx.tenant.id,
        user_id=ctx.user.id if ctx.user is not None else None,
        kind=kind,
        payload=payload,
        status="pending",
        expires_at=datetime.now(timezone.utc) + PROPOSAL_TTL,
    )
    ctx.db.add(proposal)
    await ctx.db.flush()
    return {
        "proposal_id": str(proposal.id),
        "kind": kind,
        "status": "pending",
        "preview": payload,
        "expires_at": proposal.expires_at.isoformat(),
    }


async def dispatch_tool(
    ctx: AgentContext, name: str, args: Dict[str, Any]
) -> Dict[str, Any]:
    """Execute a tool by name. Read tools run; draft tools create WriteProposal rows."""
    if name == "search_interactions":
        return await _exec_search_interactions(ctx, args)
    if name == "get_action_items":
        return await _exec_get_action_items(ctx, args)
    if name == "get_interaction_detail":
        return await _exec_get_interaction_detail(ctx, args)
    if name in DRAFT_TOOLS:
        return await _create_proposal(ctx, DRAFT_KIND_BY_TOOL[name], args)
    return {"error": f"unknown tool: {name}"}


# ── Orchestration / streaming ──────────────────────────────────────────────


async def _load_history(db: AsyncSession, conversation_id: uuid.UUID) -> List[Dict[str, Any]]:
    """Replay prior messages in Anthropic message format."""
    rows = (
        await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.asc())
        )
    ).scalars().all()
    messages: List[Dict[str, Any]] = []
    for row in rows:
        if row.role in ("user", "assistant"):
            if row.tool_calls:
                messages.append({"role": row.role, "content": row.tool_calls})
            else:
                messages.append({"role": row.role, "content": row.content})
    return messages


async def run_chat_turn(
    ctx: AgentContext, user_message: str
) -> AsyncIterator[Dict[str, Any]]:
    """Run one chat turn, yielding SSE-friendly event dicts.

    Event shapes:
      {"type": "text", "delta": str}            — assistant text delta
      {"type": "tool_use", "tool": str, "input": dict}
      {"type": "tool_result", "tool": str, "result": dict}
      {"type": "proposal", "proposal": dict}    — write proposal created
      {"type": "done"}                          — end of turn
      {"type": "error", "message": str}
    """
    client = get_async_anthropic()
    system_blocks = build_system_blocks(ctx.tenant, ctx.user)

    history = await _load_history(ctx.db, ctx.conversation_id)
    history.append({"role": "user", "content": user_message})

    # Persist the incoming user message immediately so it's durable even if the stream fails
    ctx.db.add(
        ConversationMessage(
            conversation_id=ctx.conversation_id,
            tenant_id=ctx.tenant.id,
            user_id=ctx.user.id if ctx.user is not None else None,
            role="user",
            content=user_message,
        )
    )
    await ctx.db.flush()

    max_loops = 5  # guard against runaway tool-use cycles
    for _ in range(max_loops):
        assistant_text_parts: List[str] = []
        final_content: List[Dict[str, Any]] = []

        async with client.messages.stream(
            model=LINDA_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=TOOLS,
            messages=history,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    assistant_text_parts.append(event.delta.text)
                    yield {"type": "text", "delta": event.delta.text}

            final = await stream.get_final_message()

        # Serialize assistant content blocks for history + DB
        for block in final.content:
            if block.type == "text":
                final_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                final_content.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )

        history.append({"role": "assistant", "content": final_content})

        ctx.db.add(
            ConversationMessage(
                conversation_id=ctx.conversation_id,
                tenant_id=ctx.tenant.id,
                role="assistant",
                content="".join(assistant_text_parts),
                tool_calls=final_content,
            )
        )
        await ctx.db.flush()

        if final.stop_reason != "tool_use":
            break

        # Execute tool calls and append their results for the next loop
        tool_results: List[Dict[str, Any]] = []
        for block in final.content:
            if block.type != "tool_use":
                continue
            yield {"type": "tool_use", "tool": block.name, "input": block.input}
            try:
                result = await dispatch_tool(ctx, block.name, dict(block.input))
            except Exception as exc:
                logger.exception("tool dispatch failed: %s", block.name)
                result = {"error": str(exc)}

            if block.name in DRAFT_TOOLS and "proposal_id" in result:
                yield {"type": "proposal", "proposal": result}
            else:
                yield {"type": "tool_result", "tool": block.name, "result": result}

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )

        history.append({"role": "user", "content": tool_results})
        ctx.db.add(
            ConversationMessage(
                conversation_id=ctx.conversation_id,
                tenant_id=ctx.tenant.id,
                role="tool",
                content="",
                tool_calls=tool_results,
            )
        )
        await ctx.db.flush()

    yield {"type": "done"}


async def get_or_create_conversation(
    db: AsyncSession,
    tenant: Tenant,
    user: Optional[User],
    conversation_id: Optional[uuid.UUID],
) -> Conversation:
    if conversation_id is not None:
        existing = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.tenant_id == tenant.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    convo = Conversation(
        tenant_id=tenant.id, user_id=user.id if user is not None else None
    )
    db.add(convo)
    await db.flush()
    return convo
