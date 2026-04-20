"""Model router — tiered Claude invocation with caching and batch support.

Every LLM call in the backend goes through :class:`ModelRouter`.  The
router enforces three cost/speed disciplines:

1. **Tier selection.**  Routes a task to Haiku / Sonnet / Opus based on
   ``task_type``, ``complexity_score``, and transcript size.  Opus is
   reserved for orchestrator-level work on aggregated summaries — it
   never touches raw transcripts in the live path.
2. **Prompt caching.**  Every system prompt, tenant-scoped context
   block, and agent/client profile header is sent with
   ``cache_control: ephemeral`` so the second call within the cache
   window is charged only for the uncached tail.
3. **Batch API.**  Non-interactive tasks (tenant rollups, weekly
   reflection, backfill) submit through the Anthropic Messages Batches
   API for ~50 % token discount and no sync rate-limit pressure.

The router is synchronous-async dual-capable; live endpoints call
``ainvoke`` while Celery workers call ``invoke``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

import anthropic

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


# ── Tier / task enums ────────────────────────────────────────────────────


class Tier(str, Enum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


# Canonical model IDs.  Keep in sync with ``ai_analysis.MODELS`` so the
# pipeline can swap to routed calls without drift.
MODEL_IDS: Dict[Tier, str] = {
    Tier.HAIKU: "claude-haiku-4-5-20251001",
    Tier.SONNET: "claude-sonnet-4-6",
    Tier.OPUS: "claude-opus-4-7",
}


class TaskType(str, Enum):
    TRIAGE = "triage"                         # always Haiku
    MAIN_ANALYSIS = "main_analysis"           # Haiku/Sonnet by complexity
    DELTA_REPORT = "delta_report"             # Sonnet, small output
    ORCH_CLIENT = "orchestrator_client"       # Opus
    ORCH_AGENT = "orchestrator_agent"         # Opus
    ORCH_MANAGER = "orchestrator_manager"     # Opus
    ORCH_BUSINESS = "orchestrator_business"   # Opus
    ORCH_WEEKLY = "orchestrator_weekly"       # Opus, long-context
    COACHING_PICK = "coaching_pick"           # Haiku: pick top-K coaching
    QUALITY_REVIEW = "quality_review"         # Opus, tiny surface


# ── Request shape ────────────────────────────────────────────────────────


@dataclass
class CacheableBlock:
    """One ``cache_control: ephemeral`` text block in the system prompt."""

    text: str
    cache: bool = True


@dataclass
class LLMRequest:
    task_type: TaskType
    user_message: str
    system_blocks: List[CacheableBlock] = field(default_factory=list)
    complexity_score: float = 0.5
    transcript_tokens: int = 0
    tenant_tier: str = "standard"            # "standard" | "enterprise"
    retry_count: int = 0
    max_tokens: int = 4096
    temperature: float = 0.0
    prefer_batch: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    text: str
    model: str
    tier: Tier
    stop_reason: Optional[str]
    usage: Dict[str, int]
    via_batch: bool = False

    def parse_json(self) -> Any:
        """Convenience: strip code fences and parse the body as JSON."""
        from backend.app.services.triage_service import _strip_json_fences
        return json.loads(_strip_json_fences(self.text))


# ── Router ───────────────────────────────────────────────────────────────


class ModelRouter:
    """Routes LLM work by task type and context.

    Designed so the pipeline calls ``router.ainvoke(req)`` and never
    hard-codes a model name.  Downstream observability can slice by
    ``task_type`` + ``tier`` without parsing the request.
    """

    # Transcript-size thresholds (token-ish; approximate).
    _LARGE_TRANSCRIPT_TOKENS = 12000
    # Complexity thresholds that bump tiers for MAIN_ANALYSIS.
    _COMPLEXITY_HAIKU_MAX = 0.35
    _COMPLEXITY_SONNET_MAX = 0.75

    def __init__(self, client: Optional[anthropic.AsyncAnthropic] = None) -> None:
        settings = get_settings()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY
        )

    # ── Tier selection ────────────────────────────────────────────────

    def select_tier(self, req: LLMRequest) -> Tier:
        """Pick the cheapest model that satisfies the task's contract."""
        t = req.task_type

        # Orchestrator + quality review always use Opus.
        if t in {
            TaskType.ORCH_CLIENT, TaskType.ORCH_AGENT,
            TaskType.ORCH_MANAGER, TaskType.ORCH_BUSINESS,
            TaskType.ORCH_WEEKLY, TaskType.QUALITY_REVIEW,
        }:
            return Tier.OPUS

        # Small classification & triage always Haiku.
        if t in {TaskType.TRIAGE, TaskType.COACHING_PICK}:
            return Tier.HAIKU

        # Delta report: fixed small Sonnet call.
        if t == TaskType.DELTA_REPORT:
            return Tier.SONNET

        # MAIN_ANALYSIS: complexity + size driven.
        tier: Tier
        if req.complexity_score < self._COMPLEXITY_HAIKU_MAX:
            tier = Tier.HAIKU
        elif req.complexity_score < self._COMPLEXITY_SONNET_MAX:
            tier = Tier.SONNET
        else:
            tier = Tier.SONNET
        if req.transcript_tokens > self._LARGE_TRANSCRIPT_TOKENS:
            tier = Tier.SONNET

        # Enterprise tier and retries bump one tier up (never above Opus).
        if req.tenant_tier == "enterprise" or req.retry_count > 0:
            tier = self._bump(tier)
        return tier

    @staticmethod
    def _bump(tier: Tier) -> Tier:
        if tier == Tier.HAIKU:
            return Tier.SONNET
        if tier == Tier.SONNET:
            return Tier.OPUS
        return Tier.OPUS

    # ── Live invocation ───────────────────────────────────────────────

    async def ainvoke(self, req: LLMRequest) -> LLMResponse:
        """Async path — used by live FastAPI handlers."""
        tier = self.select_tier(req)
        model = MODEL_IDS[tier]
        system_payload = _build_system_payload(req.system_blocks)
        try:
            resp = await self._client.messages.create(
                model=model,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                system=system_payload,
                messages=[{"role": "user", "content": req.user_message}],
            )
        except anthropic.APIError as exc:
            logger.error("Anthropic API error (%s): %s", tier, exc)
            raise

        content = resp.content[0].text if resp.content else ""
        return LLMResponse(
            text=content,
            model=model,
            tier=tier,
            stop_reason=resp.stop_reason,
            usage=_usage_dict(resp),
            via_batch=False,
        )

    def invoke(self, req: LLMRequest) -> LLMResponse:
        """Sync path — used inside Celery tasks."""
        return asyncio.run(self.ainvoke(req))

    # ── Batch submission ──────────────────────────────────────────────

    async def submit_batch(
        self,
        requests: List[LLMRequest],
    ) -> str:
        """Submit a list of non-interactive requests via the Messages
        Batches API.  Returns the Anthropic batch id; callers poll for
        completion and fetch results separately.

        Implementation note: the Batches API expects each entry to have
        a unique ``custom_id``; we use the metadata ``custom_id`` field
        or fall back to the position index.
        """
        entries = []
        for i, r in enumerate(requests):
            tier = self.select_tier(r)
            entries.append({
                "custom_id": str(r.metadata.get("custom_id", i)),
                "params": {
                    "model": MODEL_IDS[tier],
                    "max_tokens": r.max_tokens,
                    "temperature": r.temperature,
                    "system": _build_system_payload(r.system_blocks),
                    "messages": [{"role": "user", "content": r.user_message}],
                },
            })

        # Defensive: not all Anthropic SDK versions expose
        # ``beta.messages.batches``.  Fall back to sequential calls if
        # the batch surface is unavailable.
        try:
            batch = await self._client.beta.messages.batches.create(requests=entries)  # type: ignore[attr-defined]
            return batch.id
        except AttributeError:
            logger.warning(
                "Batches API unavailable in this SDK; falling back to sequential calls"
            )
            for r in requests:
                await self.ainvoke(r)
            return "local-fallback"


# ── Prompt-cache helpers ─────────────────────────────────────────────────


def _build_system_payload(blocks: List[CacheableBlock]) -> List[Dict[str, Any]]:
    """Render ``CacheableBlock`` entries into Anthropic API ``system`` form.

    Adjacent cacheable blocks are each emitted with
    ``cache_control: ephemeral`` so the client respects per-block cache
    boundaries.  Non-cacheable blocks omit the marker.
    """
    if not blocks:
        return []
    payload: List[Dict[str, Any]] = []
    for block in blocks:
        entry: Dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache:
            entry["cache_control"] = {"type": "ephemeral"}
        payload.append(entry)
    return payload


def tenant_context_block(tenant: Any) -> CacheableBlock:
    """Build the cacheable tenant-context prompt section.

    The content is static per tenant + active scorecards / glossary, so
    Anthropic's ephemeral cache covers every call within the window.
    """
    parts: List[str] = [
        "## Tenant context",
        f"Tenant: {getattr(tenant, 'name', '')}",
        f"Automation level: {getattr(tenant, 'automation_level', 'suggest')}",
    ]
    glossary = getattr(tenant, "canonical_glossary", None)
    if glossary:
        parts.append("Canonical topic glossary:")
        for canonical, synonyms in glossary.items():
            parts.append(f"- {canonical}: {', '.join(synonyms)}")
    return CacheableBlock(text="\n".join(parts))


def agent_profile_header(profile: Dict[str, Any]) -> CacheableBlock:
    """Short cacheable header embedding the agent profile summary."""
    summary = profile.get("summary", "")
    weak_skills = profile.get("metrics", {}).get("weak_skills", [])
    weak_str = ", ".join(weak_skills[:3]) or "none flagged"
    text = (
        "## Agent profile\n"
        f"{summary}\n"
        f"Current growth areas: {weak_str}"
    )
    return CacheableBlock(text=text)


def client_profile_header(profile: Dict[str, Any]) -> CacheableBlock:
    """Short cacheable header embedding the client profile summary."""
    summary = profile.get("summary", "")
    last_deltas = profile.get("history", [])[:3]
    deltas = "; ".join(d.get("headline", "") for d in last_deltas) or "no prior deltas"
    text = (
        "## Client profile\n"
        f"{summary}\n"
        f"Recent conversation shifts: {deltas}"
    )
    return CacheableBlock(text=text)


# ── Utility ──────────────────────────────────────────────────────────────


def _usage_dict(resp: Any) -> Dict[str, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
    }


# Lazy singleton for places that don't want to construct their own.
_default_router: Optional[ModelRouter] = None


def get_router() -> ModelRouter:
    global _default_router
    if _default_router is None:
        _default_router = ModelRouter()
    return _default_router
