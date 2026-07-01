"""Generate long-form documents for document_send action plan steps.

Today the Call C synthesizer produces a *cover note* (short email body)
plus a list of attachment titles for document_send steps. The
attachments themselves are not produced — the rep is expected to
hand-attach a file or click a "Generate document" button which routes
through this module.

The generator runs Claude Sonnet against the step's context
(description, intent, channel_reasoning, prep_artifacts, the source
interaction's summary + key moments) and returns a Markdown document
appropriate for the requested attachment title. The output is
deliberately Markdown rather than PDF: rendering happens in the browser
(MarkdownIt → HTML → ``window.print()`` for PDF). That keeps zero
heavy server-side PDF dependencies in the Python image and lets the
rep paste the output into their preferred doc editor when they want
to edit before sending.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import ActionStep, Interaction
from backend.app.services.llm_client import compute_max_tokens, get_async_anthropic
from backend.app.services.llm_telemetry import record_llm_completion

from backend.app.services import model_catalog

logger = logging.getLogger(__name__)

_SONNET = model_catalog.SONNET


_SYSTEM_PROMPT = (
    "You are drafting a long-form business document on behalf of a "
    "customer-facing rep. The document will be attached to an outbound "
    "email to the customer after the rep reviews it.\n"
    "\n"
    "Output STRICT Markdown only. No preamble, no postscript, no "
    "commentary. Start with a level-1 heading; use level-2 / level-3 "
    "for sections. Tables and bulleted lists are fine when the content "
    "is genuinely tabular or parallel. Use bold sparingly for key "
    "figures.\n"
    "\n"
    "VOICE RULES (same family as the analysis prompt)\n"
    "- Plain English. No invented jargon, no 'X, not Y' contrast tics, "
    "no all-caps section labels, no em-dashes. Use periods, colons, "
    "commas, semicolons, or parentheses.\n"
    "- Anchor every claim in the call evidence (specific names, "
    "numbers, quoted commitments). Do not invent facts the rep did "
    "not surface on the call.\n"
    "- Length: roughly one printed page (500-900 words). Concise but "
    "substantive. The rep should be able to send it as-is or with a "
    "small edit pass.\n"
    "- Tone: confident and grounded, not salesy. This is a working "
    "document, not a pitch.\n"
)


@dataclass
class GeneratedDocument:
    title: str
    body_markdown: str
    word_count: int
    model: str
    generated_at_unix: float


def _context_block_from_interaction(interaction: Interaction) -> str:
    """Produce a compact context block from the interaction's insights
    that grounds the document in what the call actually said."""
    insights = interaction.insights or {}
    parts: List[str] = []
    summary = insights.get("summary")
    if summary:
        parts.append(f"## Call summary\n{summary}\n")
    key_moments = insights.get("key_moments") or []
    if isinstance(key_moments, list) and key_moments:
        lines = []
        for km in key_moments[:8]:
            if not isinstance(km, dict):
                continue
            desc = km.get("description")
            if desc:
                lines.append(f"- {desc}")
        if lines:
            parts.append("## Key moments\n" + "\n".join(lines) + "\n")
    customer_signals = insights.get("customer_signals") or {}
    if isinstance(customer_signals, dict):
        commitments = customer_signals.get("commitment_language") or []
        if isinstance(commitments, list) and commitments:
            parts.append(
                "## Customer commitments (verbatim quotes)\n"
                + "\n".join(f"- {q}" for q in commitments[:6])
                + "\n"
            )
    rubric = insights.get("rubric") or {}
    if isinstance(rubric, dict) and rubric:
        parts.append(
            "## Call rubric\n"
            + "\n".join(
                f"- {k}: {v}" for k, v in rubric.items() if v is not None
            )
            + "\n"
        )
    return "\n".join(parts) if parts else "(no analysis insights available)"


def _step_context_block(step: ActionStep) -> str:
    parts: List[str] = []
    parts.append(f"## Step\n{step.title}")
    if step.description:
        parts.append(f"\n### Description\n{step.description}")
    if step.intent:
        parts.append(f"\n### Intent\n{step.intent}")
    if step.channel_reasoning:
        parts.append(f"\n### Why this channel\n{step.channel_reasoning}")
    if step.prep_artifacts:
        artifact_lines = [
            f"- {p}" for p in step.prep_artifacts
            if isinstance(p, str) and p.strip()
        ]
        if artifact_lines:
            parts.append(
                "\n### Prep artifacts the rep planned to include\n"
                + "\n".join(artifact_lines)
            )
    if step.participants:
        names = ", ".join(
            str(p.get("name", "")) for p in step.participants if isinstance(p, dict)
        )
        if names:
            parts.append(f"\n### Participants\n{names}")
    return "\n".join(parts)


async def generate_document_for_step(
    db: AsyncSession,
    *,
    step: ActionStep,
    interaction: Optional[Interaction],
    attachment_title: Optional[str] = None,
    extra_instructions: Optional[str] = None,
) -> GeneratedDocument:
    """Generate one document for an action plan step.

    ``attachment_title`` selects which of the synthesizer's suggested
    attachments to render (e.g. "ROI Model", "Pilot Scope"). When
    omitted, the title defaults to ``step.title`` so the user can
    still invoke the generator on plain ``document_send`` steps that
    didn't enumerate distinct attachments.
    """
    title = (attachment_title or step.title or "Document").strip()
    step_block = _step_context_block(step)
    call_block = (
        _context_block_from_interaction(interaction) if interaction is not None
        else "(no source interaction available)"
    )

    user_message = (
        f"Document to produce: **{title}**\n\n"
        f"{step_block}\n\n"
        "## Source call context (factual ground truth)\n"
        f"{call_block}\n"
    )
    if extra_instructions:
        user_message += f"\n## Additional instructions from the rep\n{extra_instructions}\n"
    user_message += (
        "\nReturn ONLY the Markdown document body. Do not wrap in code "
        "fences or include any meta-commentary."
    )

    client = get_async_anthropic()
    max_tokens = compute_max_tokens(
        "sonnet",
        input_tokens=len(user_message) // 4,
        task_type="document_generation",
        explicit_override=4096,
        call_site="action_plan_document_generator",
    )
    started = time.monotonic()
    response = await client.messages.create(
        model=_SONNET,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )
    record_llm_completion("action_plan_document_generator", "sonnet", max_tokens, response)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    body = ""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            body += getattr(block, "text", "") or ""
    body = body.strip()
    if body.startswith("```"):
        # Defensive: strip a fenced wrapper if the model emitted one.
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.endswith("```"):
            body = body.rsplit("```", 1)[0]
        body = body.strip()

    word_count = len(body.split())
    logger.info(
        "Generated document for step %s: %d words, %d ms, max_tokens=%d",
        step.id, word_count, elapsed_ms, max_tokens,
    )
    return GeneratedDocument(
        title=title,
        body_markdown=body,
        word_count=word_count,
        model=_SONNET,
        generated_at_unix=time.time(),
    )


__all__ = ["GeneratedDocument", "generate_document_for_step"]
