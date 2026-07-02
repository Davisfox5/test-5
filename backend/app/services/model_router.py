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
3. **Batch API.**  ``submit_batch`` / ``run_batch`` wrap the Anthropic
   Messages Batches API (~50 % token discount, no sync rate-limit
   pressure) with the same tier-pinning, request shaping, and failover
   as the live path.  No production task submits through it yet —
   non-interactive work (tenant rollups, weekly reflection, backfill)
   currently calls ``invoke``; moving it onto ``run_batch`` is an open
   cost optimization.

The router is synchronous-async dual-capable; live endpoints call
``ainvoke`` while Celery workers call ``invoke``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

import anthropic

from backend.app.services import model_catalog


logger = logging.getLogger(__name__)


# ── Tier / task enums ────────────────────────────────────────────────────


class Tier(str, Enum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


# Canonical model IDs — resolved from the single source of truth in
# ``model_catalog`` (which reads env-overridable ids from config). Keeping
# the enum-keyed dict shape here so existing callers (and tests) are
# unchanged; the *values* now have exactly one owner.
MODEL_IDS: Dict[Tier, str] = {
    Tier.HAIKU: model_catalog.HAIKU,
    Tier.SONNET: model_catalog.SONNET,
    Tier.OPUS: model_catalog.OPUS,
}

class TaskType(str, Enum):
    GENERIC = "generic"                       # tier chosen by forced_tier
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
    # Explicit tier pin. When set, ``select_tier`` returns it verbatim — this is
    # how a migrated call site keeps the exact model choice it used to hardcode,
    # instead of inventing a TaskType for every one of them.
    forced_tier: Optional[Tier] = None
    # Full message list (multi-turn / tool-use history). When set it is used
    # verbatim; otherwise the router sends ``[{"role":"user", user_message}]``.
    messages: Optional[List[Dict[str, Any]]] = None
    # Anthropic tool definitions, forwarded when present (tool-use surfaces).
    tools: Optional[List[Dict[str, Any]]] = None
    # Explicit thinking config (e.g. ``{"type": "adaptive"}``). When set it is
    # forwarded verbatim. When unset, the router suppresses thinking on models
    # that default it on (Sonnet 5) — see ``_build_create_kwargs``.
    thinking: Optional[Dict[str, Any]] = None
    # Stable telemetry key (e.g. ``"kb_classifier"``). When set, the router
    # records the completion to ``llm_call_telemetry`` — one recording site for
    # every routed call, so observability is uniform without per-site wiring.
    call_site: Optional[str] = None
    # Per-request timeout override (seconds), forwarded when set. Preserves
    # call sites that built their own client with a custom timeout.
    timeout: Optional[float] = None
    retry_count: int = 0
    # Why this retry is happening, when known. ``"max_tokens"`` and
    # ``"context_length"`` legitimately need a more capable model;
    # everything else (transient 5xx, rate-limit, network) should retry
    # on the same tier with backoff. Default ``None`` keeps existing
    # callers safe — old ``retry_count > 0`` no longer auto-escalates.
    retry_reason: Optional[str] = None
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
        from backend.app.services.llm_client import get_async_anthropic

        self._client = client or get_async_anthropic()

    # ── Tier selection ────────────────────────────────────────────────

    def select_tier(self, req: LLMRequest) -> Tier:
        """Pick the cheapest model that satisfies the task's contract."""
        # An explicit pin wins over task-based selection.
        if req.forced_tier is not None:
            return req.forced_tier
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

        # Enterprise tier always bumps. Retries only escalate when the
        # failure mode actually warrants a more capable model — output
        # truncation or context-length overflow. Transient errors retry
        # at the same tier (with backoff handled by the caller).
        _ESCALATING_RETRY_REASONS = {"max_tokens", "context_length"}
        if req.tenant_tier == "enterprise":
            tier = self._bump(tier)
        elif req.retry_count > 0 and req.retry_reason in _ESCALATING_RETRY_REASONS:
            tier = self._bump(tier)
        elif req.retry_count > 0 and req.retry_reason is None:
            # Surface stale call sites that still rely on the old "any
            # retry → bump" behavior. One-release transition warning.
            logger.warning(
                "model_router retry without retry_reason; staying on %s "
                "(set req.retry_reason to escalate)",
                tier,
            )
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
        from backend.app.services.llm_client import acreate_with_failover

        tier = self.select_tier(req)
        model = MODEL_IDS[tier]
        # Bounded transient retries + one model failover to the cheaper tier.
        # kwargs are shared across the failover, so shape them for BOTH
        # models (see the thinking note in ``_build_create_kwargs``).
        fallback = model_catalog.failover_model(tier.value)
        create_kwargs = self._build_create_kwargs(req, model, fallback_model=fallback)
        try:
            resp = await acreate_with_failover(
                self._client, model=model, fallback_model=fallback, **create_kwargs
            )
        except anthropic.APIError as exc:
            logger.error("Anthropic API error (%s): %s", tier, exc)
            raise
        # The failover wrapper may have served the request on a cheaper tier;
        # report the model AND tier the response actually came from so both the
        # LLMResponse and the telemetry below stay accurate after a failover.
        model = getattr(resp, "model", model) or model
        served_tier = model_catalog.tier_for_model(model)
        if served_tier is not None:
            tier = Tier(served_tier)
        # One uniform telemetry recording site for every routed call.
        self._record(req, tier, resp)

        content = resp.content[0].text if resp.content else ""
        return LLMResponse(
            text=content,
            model=model,
            tier=tier,
            stop_reason=getattr(resp, "stop_reason", None),
            usage=_usage_dict(resp),
            via_batch=False,
        )

    # ── Request shaping / telemetry helpers ───────────────────────────────

    def _build_create_kwargs(
        self,
        req: LLMRequest,
        model: str,
        fallback_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Assemble ``messages.create`` kwargs shared by the sync and stream
        paths (model is passed separately by the failover wrapper / stream).

        ``fallback_model`` matters when the same kwargs are reused verbatim
        across a failover (the ``ainvoke`` path): an Opus request needs no
        thinking suppression, but if it fails over to Sonnet 5 the fallback
        would silently run adaptive thinking — eating the ``max_tokens``
        budget and adding latency.  So thinking is suppressed when EITHER
        model in the failover chain defaults it on (``disabled`` is accepted
        by every runtime model)."""
        kwargs: Dict[str, Any] = {
            "max_tokens": req.max_tokens,
            "system": _build_system_payload(req.system_blocks),
            "messages": req.messages or [{"role": "user", "content": req.user_message}],
        }
        if req.tools:
            kwargs["tools"] = req.tools
        if req.timeout is not None:
            kwargs["timeout"] = req.timeout
        # Thinking: honor an explicit opt-in; otherwise disable it on models
        # that default adaptive thinking on (Sonnet 5) so it doesn't eat into
        # ``max_tokens`` or add latency — preserving prior no-thinking behavior.
        if req.thinking is not None:
            kwargs["thinking"] = req.thinking
        elif any(
            model_catalog.thinking_on_by_default(m)
            for m in (model, fallback_model)
            if m
        ):
            kwargs["thinking"] = {"type": "disabled"}
        # ``temperature`` 400s on Opus 4.7+/Fable 5; only send it to models that
        # still accept sampling params. Failover only ever steps DOWN a tier
        # (opus→sonnet→haiku) and the lower tiers accept temperature, so a
        # stripped-temperature request stays valid after failover.
        if not model_catalog.rejects_sampling_params(model):
            kwargs["temperature"] = req.temperature
        return kwargs

    def _record(self, req: LLMRequest, tier: Tier, resp: Any) -> None:
        """Fire-and-forget completion telemetry, keyed on ``req.call_site``."""
        if not req.call_site:
            return
        try:
            from backend.app.services import llm_telemetry

            llm_telemetry.record_llm_completion(
                req.call_site, tier.value, req.max_tokens, resp,
                tenant_id=req.metadata.get("tenant_id") if req.metadata else None,
            )
        except Exception:  # pragma: no cover — telemetry must never break a call
            logger.debug("router telemetry record failed", exc_info=True)

    def invoke(self, req: LLMRequest) -> LLMResponse:
        """Sync path — used inside Celery tasks."""
        return asyncio.run(self.ainvoke(req))

    # ── Streaming invocation (tool-use surfaces) ──────────────────────────

    @asynccontextmanager
    async def astream(self, req: LLMRequest):
        """Async-context-manager wrapper over ``client.messages.stream`` that
        routes model selection (and cache/tool/temperature shaping) through the
        router, so streaming tool-use surfaces (Ask Linda) no longer hardcode a
        model. The caller keeps its own tool-dispatch loop::

            async with router.astream(req) as stream:
                async for event in stream:
                    ...
                final = await stream.get_final_message()

        Failover is applied to the stream *open* only (a model that's
        unavailable / a transient blip at connect time steps down one tier).
        Mid-stream errors are the caller's to handle — retrying a partially
        emitted stream isn't safe — so exceptions raised while the caller
        iterates propagate untouched.
        """
        from backend.app.services.llm_client import (
            _is_model_unavailable,
            _is_transient,
        )

        tier = self.select_tier(req)
        primary = MODEL_IDS[tier]
        fallback = model_catalog.failover_model(tier.value)
        candidates = [primary] + ([fallback] if fallback else [])

        stream = None
        cm = None
        for idx, candidate in enumerate(candidates):
            cm = self._client.messages.stream(
                model=candidate, **self._build_create_kwargs(req, candidate)
            )
            try:
                # Enter separately from the yield so ONLY open failures trigger
                # failover — caller-side iteration errors must not.
                stream = await cm.__aenter__()
                break
            except Exception as exc:  # noqa: BLE001 — classified below
                is_last = idx == len(candidates) - 1
                if not is_last and (_is_model_unavailable(exc) or _is_transient(exc)):
                    logger.warning(
                        "stream open failed on %s (%s); failover to %s",
                        candidate, type(exc).__name__, candidates[idx + 1],
                    )
                    continue
                raise
        try:
            yield stream
        finally:
            if cm is not None:
                await cm.__aexit__(None, None, None)

    # ── Batch submission ──────────────────────────────────────────────

    @staticmethod
    def _custom_id_for(req: LLMRequest, index: int) -> str:
        """Stable per-entry id: the caller's ``metadata['custom_id']`` when set,
        else the position index. Reused verbatim across a failover round so
        results merge back by id."""
        return str(req.metadata.get("custom_id", index) if req.metadata else index)

    def _batch_entry(self, req: LLMRequest, model: str, custom_id: str) -> Dict[str, Any]:
        """Build one Batches API entry, sharing the live path's request shaping
        (``_build_create_kwargs``) so batch calls inherit the same temperature
        guard, Sonnet-5 thinking suppression, and tool passthrough. ``timeout``
        is a client-side SDK option, not a wire param, so it's dropped here."""
        params = self._build_create_kwargs(req, model)
        params.pop("timeout", None)
        params["model"] = model
        return {"custom_id": custom_id, "params": params}

    async def _create_batch(
        self,
        entries: List[Dict[str, Any]],
        *,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> str:
        """POST a batch with bounded transient retry. Raises ``AttributeError``
        when the SDK lacks ``beta.messages.batches`` so the caller can fall
        back to sequential live calls."""
        from backend.app.services.llm_client import _is_transient

        attempt = 0
        while True:
            try:
                batch = await self._client.beta.messages.batches.create(requests=entries)  # type: ignore[attr-defined]
                return batch.id
            except AttributeError:
                raise
            except Exception as exc:  # noqa: BLE001 — classified below
                if _is_transient(exc) and attempt < 2:
                    delay = 0.5 * (2 ** attempt)
                    logger.warning(
                        "batch submit transient error (%s, attempt %d/2); retry in %.2fs",
                        type(exc).__name__, attempt + 1, delay,
                    )
                    await _sleep(delay)
                    attempt += 1
                    continue
                raise

    async def submit_batch(
        self,
        requests: List[LLMRequest],
    ) -> str:
        """Submit a list of non-interactive requests via the Messages
        Batches API.  Returns the Anthropic batch id; callers poll for
        completion and fetch results separately (or use :meth:`run_batch`
        for the submit → poll → per-entry failover lifecycle in one call).

        Implementation note: the Batches API expects each entry to have
        a unique ``custom_id``; we use the metadata ``custom_id`` field
        or fall back to the position index.
        """
        entries = [
            self._batch_entry(r, MODEL_IDS[self.select_tier(r)], self._custom_id_for(r, i))
            for i, r in enumerate(requests)
        ]
        # Defensive: not all Anthropic SDK versions expose
        # ``beta.messages.batches``.  Fall back to sequential calls if
        # the batch surface is unavailable.
        try:
            return await self._create_batch(entries)
        except AttributeError:
            logger.warning(
                "Batches API unavailable in this SDK; falling back to sequential calls"
            )
            for r in requests:
                await self.ainvoke(r)
            return "local-fallback"

    async def run_batch(
        self,
        requests: List[LLMRequest],
        *,
        poll_interval_seconds: float = 30.0,
        timeout_seconds: float = 1800.0,
    ) -> Dict[str, Dict[str, Any]]:
        """Submit → poll → per-entry failover → merge, the batch-path analogue
        of :meth:`ainvoke`'s retry+failover.

        Returns ``{custom_id: {"text", "usage", "stop_reason"}}`` (plus an
        ``"error"`` key on entries that stayed failed). Entries that come back
        errored with a *retryable* reason (transient overload/timeout or a
        model that was unavailable) are resubmitted **once** on the fallback
        tier — one step down, never up — and successful retries overwrite the
        errored result. Deterministic client errors are left as-is.

        Every submitted ``custom_id`` is present in the result. Entries whose
        results never arrived (poll timeout, results-fetch failure) come back
        with ``error type "result_unavailable"`` — NOT retried on a lower
        tier, because the original batch may still be running server-side and
        resubmitting would risk paying for both.

        The failover round is best-effort: if its submit/poll itself fails,
        the first round's results are returned as-is (errored entries intact)
        rather than discarding work already paid for. Worst-case wall time is
        therefore ~``2 × timeout_seconds`` (two full poll windows).

        Successful entries are recorded to ``llm_call_telemetry`` (keyed on
        each request's ``call_site``, same as the live path).

        Falls back to sequential :meth:`ainvoke` (which has its own
        retry+failover) when the SDK lacks the Batches surface, so the result
        dict is populated either way.
        """
        # Remember the tier each id was submitted on so failover knows where to
        # step down to.
        plan = [
            (self._custom_id_for(r, i), r, self.select_tier(r))
            for i, r in enumerate(requests)
        ]
        entries = [self._batch_entry(r, MODEL_IDS[tier], cid) for cid, r, tier in plan]

        try:
            batch_id = await self._create_batch(entries)
        except AttributeError:
            logger.warning(
                "Batches API unavailable in this SDK; run_batch falling back to sequential calls"
            )
            return await self._run_batch_sequential(plan)

        results = await self.fetch_batch_results(
            batch_id,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
        )

        # Every submitted id must appear in the result. Ids the poll never
        # produced (timeout / results-fetch failure) are surfaced as errored
        # rather than silently dropped — and deliberately NOT classified
        # retryable: the original batch may still be running server-side, so a
        # resubmit could pay for the work twice.
        missing = [cid for cid, _, _ in plan if cid not in results]
        if missing:
            logger.warning(
                "batch %s: %d/%d entries have no result (poll timeout or fetch "
                "failure); marking result_unavailable", batch_id, len(missing), len(plan),
            )
            for cid in missing:
                results[cid] = {
                    "text": "", "usage": {}, "stop_reason": None,
                    "error": {"type": "result_unavailable"},
                }

        # Collect entries that errored retryably and have a lower tier to try.
        from backend.app.services.llm_client import batch_error_is_retryable

        retry_plan = []
        for cid, r, tier in plan:
            res = results.get(cid)
            err = res.get("error") if res else None
            if not err:
                self._record_batch_entry(r, tier, res)
                continue
            if not batch_error_is_retryable(err.get("type")):
                continue
            fb = model_catalog.failover_tier(tier.value)
            if fb:
                retry_plan.append((cid, r, Tier(fb)))

        if not retry_plan:
            return results

        logger.warning(
            "batch %s: %d/%d entries errored retryably; failover round on lower tier",
            batch_id, len(retry_plan), len(plan),
        )
        retry_entries = [
            self._batch_entry(r, MODEL_IDS[tier], cid) for cid, r, tier in retry_plan
        ]
        retry_self_recorded = False
        try:
            retry_batch_id = await self._create_batch(retry_entries)
            retry_results = await self.fetch_batch_results(
                retry_batch_id,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
            )
        except AttributeError:
            # SDK lost the surface between rounds (shouldn't happen); retry the
            # failed subset sequentially rather than leaving them errored.
            # ainvoke records its own telemetry, so skip the per-entry record.
            retry_results = await self._run_batch_sequential(retry_plan)
            retry_self_recorded = True
        except Exception:  # noqa: BLE001 — failover round is best-effort
            # The retry round's own infrastructure failed. The first round's
            # results are already paid for — return them (errored entries
            # intact) instead of raising and discarding completed work.
            logger.exception(
                "batch %s: failover round submit/poll failed; returning "
                "first-round results", batch_id,
            )
            return results

        # Overwrite only entries whose retry actually succeeded; a still-errored
        # retry keeps the original error so the caller sees the real failure.
        for cid, r, tier in retry_plan:
            rr = retry_results.get(cid)
            if rr and not rr.get("error"):
                results[cid] = rr
                if not retry_self_recorded:
                    self._record_batch_entry(r, tier, rr)
        return results

    def _record_batch_entry(
        self, req: LLMRequest, tier: Tier, res: Dict[str, Any]
    ) -> None:
        """Feed one successful batch entry through the router's uniform
        telemetry site. Shaped like an SDK response so ``_record`` /
        ``_extract_usage`` read it the same way as a live completion."""
        self._record(req, tier, {
            "model": MODEL_IDS[tier],
            "usage": res.get("usage") or {},
            "stop_reason": res.get("stop_reason"),
        })

    async def _run_batch_sequential(
        self, plan: List[Any]
    ) -> Dict[str, Dict[str, Any]]:
        """Fallback lifecycle when the Batches SDK surface is missing: run each
        request through the live path (its own retry+failover) and shape the
        results like ``fetch_batch_results``."""
        results: Dict[str, Dict[str, Any]] = {}
        for cid, r, tier in plan:
            try:
                resp = await self.ainvoke(r)
                results[cid] = {
                    "text": resp.text,
                    "usage": resp.usage or {},
                    "stop_reason": resp.stop_reason,
                }
            except Exception as exc:  # noqa: BLE001 — record, don't abort the batch
                logger.warning("sequential batch entry %s failed: %s", cid, exc)
                results[cid] = {
                    "text": "", "usage": {}, "stop_reason": None,
                    "error": {"type": type(exc).__name__},
                }
        return results

    async def fetch_batch_results(
        self, batch_id: str, *, poll_interval_seconds: float = 30.0,
        timeout_seconds: float = 1800.0,
    ) -> Dict[str, Dict[str, Any]]:
        """Block until an Anthropic Messages Batches job completes, then
        return a dict ``{custom_id: {"text": str, "usage": dict,
        "stop_reason": str|None}}``.

        Falls back to an empty dict if the SDK doesn't expose
        ``beta.messages.batches`` or the batch is the ``local-fallback``
        sentinel returned by :meth:`submit_batch`.

        The Anthropic Batches API normally completes within minutes; the
        30-min default ceiling accommodates rare slow runs without holding
        a Celery worker forever. Polling interval of 30 s keeps the
        request count low (Anthropic doesn't bill polls separately).
        """
        if batch_id == "local-fallback":
            return {}

        elapsed = 0.0
        try:
            while True:
                batch = await self._client.beta.messages.batches.retrieve(batch_id)  # type: ignore[attr-defined]
                status = getattr(batch, "processing_status", None) or getattr(batch, "status", "")
                if status == "ended":
                    break
                if elapsed >= timeout_seconds:
                    logger.warning(
                        "Batch %s still %s after %.0fs; bailing", batch_id, status, elapsed
                    )
                    return {}
                await asyncio.sleep(poll_interval_seconds)
                elapsed += poll_interval_seconds

            results: Dict[str, Dict[str, Any]] = {}
            # results() returns an async iterator of JSONL entries.
            async for entry in await self._client.beta.messages.batches.results(batch_id):  # type: ignore[attr-defined]
                custom_id = getattr(entry, "custom_id", None) or (
                    entry.get("custom_id") if isinstance(entry, dict) else None
                )
                if not custom_id:
                    continue
                # Each entry has a ``result`` payload — either succeeded
                # or errored. We unpack only the success path; errors get
                # an empty text so callers can fall back per-row.
                result = getattr(entry, "result", None) or (
                    entry.get("result") if isinstance(entry, dict) else None
                )
                if result is None:
                    results[custom_id] = {
                        "text": "", "usage": {}, "stop_reason": None,
                        "error": {"type": "unknown"},
                    }
                    continue
                rtype = getattr(result, "type", None) or (
                    result.get("type") if isinstance(result, dict) else None
                )
                if rtype != "succeeded":
                    # Surface the error type so run_batch can decide whether the
                    # entry is worth a failover round. ``errored`` carries a
                    # nested ``error.type`` (``overloaded_error`` etc.);
                    # ``canceled`` / ``expired`` use the result type itself.
                    results[custom_id] = {
                        "text": "", "usage": {}, "stop_reason": None,
                        "error": {"type": _batch_error_type(result, rtype)},
                    }
                    continue
                message = getattr(result, "message", None) or (
                    result.get("message") if isinstance(result, dict) else None
                )
                content_blocks = (
                    getattr(message, "content", None)
                    or (message.get("content") if isinstance(message, dict) else None)
                    or []
                )
                text = ""
                for block in content_blocks:
                    btext = getattr(block, "text", None) or (
                        block.get("text") if isinstance(block, dict) else ""
                    )
                    if btext:
                        text += btext
                stop_reason = getattr(message, "stop_reason", None) or (
                    message.get("stop_reason") if isinstance(message, dict) else None
                )
                results[custom_id] = {
                    "text": text,
                    "usage": _usage_dict(message) if message is not None else {},
                    "stop_reason": stop_reason,
                }
            return results
        except AttributeError:
            logger.warning("Batches results API unavailable")
            return {}
        except Exception:
            logger.exception("Failed to fetch batch results for %s", batch_id)
            return {}


# ── Batch-result helpers ─────────────────────────────────────────────────


def _batch_error_type(result: Any, rtype: Optional[str]) -> Optional[str]:
    """Pull the failure reason off a non-succeeded batch result entry.

    ``errored`` results carry a nested ``error.type`` (``overloaded_error``,
    ``not_found_error``, …); ``canceled`` / ``expired`` have no nested error, so
    the result type itself is the reason. Tolerates both SDK objects and raw
    dicts (the results iterator shape varies across SDK versions)."""
    if rtype == "errored":
        error = getattr(result, "error", None) or (
            result.get("error") if isinstance(result, dict) else None
        )
        if error is not None:
            return getattr(error, "type", None) or (
                error.get("type") if isinstance(error, dict) else None
            )
    return rtype


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
    """Extract token usage from an SDK object OR a raw dict — the Batches
    results iterator yields plain dicts on some SDK versions."""
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage is None:
        return {}

    def _u(field: str) -> int:
        if isinstance(usage, dict):
            return int(usage.get(field, 0) or 0)
        return int(getattr(usage, field, 0) or 0)

    return {
        "input_tokens": _u("input_tokens"),
        "output_tokens": _u("output_tokens"),
        "cache_read_input_tokens": _u("cache_read_input_tokens"),
        "cache_creation_input_tokens": _u("cache_creation_input_tokens"),
    }


# Lazy singleton for places that don't want to construct their own.
_default_router: Optional[ModelRouter] = None


def get_router() -> ModelRouter:
    global _default_router
    if _default_router is None:
        _default_router = ModelRouter()
    return _default_router
