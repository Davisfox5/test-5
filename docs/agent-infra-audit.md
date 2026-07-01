# Agent-Infrastructure Audit — LINDA backend

> Applies the framework in [`agent-infrastructure-knowledge-base.md`](../agent-infrastructure-knowledge-base.md)
> inward to this repo. Two layers are kept separate:
> **Layer A** = how the shipped app selects/calls an LLM at runtime (must be flawless);
> **Layer B** = how Claude Code spends tokens when developing THIS repo (must be cost-efficient; lives under `.claude/`, never changes runtime behavior).
>
> Date: 2026-07-01. Scope: `backend/` + `scripts/` (excludes `tests/` and `.claude/worktrees/`, which belong to other sessions).

---

## Executive summary

The backend already has a **mature harness**: a central Anthropic client factory, a tiered `ModelRouter`, a centralized `max_tokens` policy with learned ceilings, a **grounded** LLM-as-judge eval layer, Prometheus + telemetry observability, and strong security/governance. Feedback loops are already **bounded and grounded** — no unbounded-retry or coherence-trap issues were found.

The dominant gap is **model-agnosticism**. A well-designed routing seam (`ModelRouter`) exists but is wired into exactly **one** consumer (`orchestrator.py`). The other ~24 LLM touchpoints bypass it and pin a **hardcoded model-id string** locally — **27 occurrences across ~25 files**, plus **three separate tier→model-id maps that must be hand-kept-in-sync**. A single model deprecation/suspension (the June 2026 Fable 5 event is the illustrative risk) would today require edits in ~25 files. This is the highest-severity, lowest-risk thing to fix, and Phase 3 fixes it.

Severity legend: **P0** breaks prod on a model change · **P1** real risk / cost · **P2** hygiene.

---

## (a) Harness gaps

Six components (present ✓ / partial ◐ / absent ✗):

| Component | State | Evidence / notes |
|---|---|---|
| State & persistence | ✓ | Postgres (`models.py`, alembic), Celery (`tasks.py`), durable chat rows (`LindaChatMessage` persisted before stream), `WriteProposal` staging, batch poll/resume in `model_router.fetch_batch_results`. |
| Security & governance | ✓ | `auth.py` scopes/`require_scope`, `audit_log.py`, `pii_redaction.py`, `token_crypto.py`, `entitlements.py`. Strong. |
| Orchestration & tool use | ✓ | `orchestrator.py` (Opus tiers via router), `linda_agent.py` (tool registry + bounded tool-use loop), `model_router.py`. |
| Memory | ◐ | Semantic/episodic via KB retrieval + `customer_memory.py` + `tenant_cache.py`; working memory via prompt caching. **Gap:** Linda chat working memory is unbounded — see (d). |
| Observability | ◐ | `llm_telemetry.record_llm_completion`, `metrics.LLM_LATENCY` (Prometheus), `observability.py`. **Gap:** telemetry/labels are attached per-call-site by hand, so coverage is uneven and you cannot uniformly slice cost/latency by (model, version) — see (e). |
| Evals | ✓ | `llm_judge.py` (grounded, isolated from execution), `regression_watchdog.py`, `calibration.py`, `insight_quality_scores`. |

**Finding H1 — P1 — the routing seam is real but nearly unused.**
`model_router.py:3` documents "Every LLM call in the backend goes through `ModelRouter`," but the only caller is `orchestrator.py:36-122,431,550`. Every other touchpoint constructs the shared client (`get_async_anthropic()`) and calls `messages.create` with a locally hardcoded model string. The seam exists; it just isn't the interface most code uses. Maps to: *harness should be the stable, model-independent interface.*

---

## (b) Layer A — routing / model-agnosticism gaps

### B1 — P0 — Model ids are scattered constants, not one seam.
**27 hardcoded `claude-*` strings across ~25 files.** Blast radius of one model id being deprecated/suspended = edit every file below. There is **no config/env override**: `config.py` has `ANTHROPIC_API_KEY` but zero model-version settings, so model choice is a compile-time constant, not a runtime decision.

Canonical (already semi-central):
- `model_router.py:49-51` — `MODEL_IDS = {HAIKU: claude-haiku-4-5-20251001, SONNET: claude-sonnet-4-6, OPUS: claude-opus-4-8}`

Duplicate tier→id maps that must be hand-synced (drift risk — `ai_analysis.MODELS` is **already missing `opus`**):
- `ai_analysis.py:23-25` — `MODELS = {haiku, sonnet}`  (comment literally says "Keep in sync with `ai_analysis.MODELS`")
- `action_plan/synthesizer.py:84-85` — `_MODELS = {haiku, sonnet}`

Per-service single-model constants:
- `triage_service.py:15`, `scorecard_service.py:28`, `manager_recommendation_builder.py:43`, `email_classifier.py:38`, `llm_judge.py:40`, `live_coaching.py:19`, `linda_agent.py:37`, `email_reply.py:47`, `action_plan/document_generator.py:37`, `action_plan/extractor.py:40`
- KB family (all Haiku): `kb/classifier.py:34`, `kb/context_builder.py:40`, `kb/customer_brief_builder.py:62`, `kb/infer_from_sources.py:54`, `kb/onboarding_interview.py:57`, `kb/tenant_brief_refiner.py:46`, `kb/orchestrator.py:60`
- Inline literals: `api/emails.py:294`, `entity_resolution.py:456`, `text_segmenter.py:197`, `warnings_commitments.py:547,667`

DB defaults (lower stakes, but same string): `backend/scripts/seed_sales.py:118`, `alembic/versions/550a40162883_initial_schema.py:504`.

**Fix (Phase 3):** one `model_catalog.py` resolves every tier→id from `config.py` (env-overridable, defaults pinned to today's ids). All the constants above import from it, so a version bump / deprecation swap is a **one-line, reviewable change**. Maps to: *make model choice a single runtime seam with a fallback.*

### B2 — P1 — No failover / transient-error handling on the model call.
On error/rate-limit/timeout, `model_router.ainvoke` (`model_router.py:230`) logs and re-raises; `ai_analysis` returns an `{"error": ...}` dict; `llm_judge` returns `None`. There is **no retry-with-backoff on transient (429/5xx/overloaded/timeout) errors and no model failover**, and failovers aren't logged (there are none). A provider blip = a failed customer analysis. Maps to: *a router adds a failure surface; it needs timeouts and a default-safe fallback.*
**Fix (Phase 3):** a bounded `safe_create()` retry/failover wrapper wired into the central router path, logging every retry/failover with its reason.

### B3 — Tier assignments (mostly a strength; one note).
De-facto tiering is sensible and cost-aware: triage/classify/KB/extraction/coaching → **Haiku**; analysis → **Haiku or Sonnet by complexity**; Linda chat & email reply (customer-facing) → **Sonnet**; orchestrator rollups & quality review (aggregated, high-stakes) → **Opus**. **No egregious over-provisioning to Opus was found.** Minor: `QUALITY_REVIEW` = Opus on a "tiny surface" (`model_router.py:79`) could be Sonnet, but it gates quality, so Opus is defensible — left as a note, not a change.

### B4 — P1 — Version currency: Sonnet is a major version behind.
Sonnet touchpoints pin `claude-sonnet-4-6`; the current Sonnet is `claude-sonnet-5`. Haiku (`claude-haiku-4-5-20251001`) and Opus (`claude-opus-4-8`) are current. This is **not** auto-fixed here (a version bump is a behavior change and the user's call). After Phase 3 centralization it becomes a **one-line** change in `config.py`/env instead of a 7-file sweep. Recommendation, not committed.

---

## (c) Feedback-loop risks

All LLM-adjacent loops are **already bounded and grounded** — this is a strength, recorded so it isn't "fixed" needlessly:

- `ai_analysis.py:1196-1232` — retry-on-truncation. **Bounded** (exactly one retry, capped at 16 384) and **grounded** (fires on `stop_reason == "max_tokens"`, an external signal). ✓
- `action_plan/synthesizer.py:_call_with_retry (~888-950)` — 2 attempts max; attempt 2 adds a stricter JSON reminder. **Bounded** and **grounded** (fires on `JSONDecodeError`, i.e. real parse failure). ✓
- `linda_agent.py:441-533` — agentic tool-use loop. **Bounded** (`max_loops = 5`) and **grounded** (terminates on `stop_reason != "tool_use"`). ✓
- `llm_judge.py` — LLM-as-judge. **Grounded** (scores against the source transcript / cited KB snippets / an edit-distance ground-truth dimension) and **isolated from execution** (runs async after the producer; writes to a separate table). Not a coherence-trap self-critique. ✓

**No ungrounded self-critique and no unbounded loop were found**, so Phase 3 does not invent one. The only loop-adjacent hardening added is around *transient errors* (B2), not iteration count.

---

## (d) Context-rot risks

### D1 — P1 — Linda chat replays unbounded history.
`linda_agent._load_history` (`linda_agent.py:391`) selects **all** `LindaChatMessage` rows for a conversation with **no LIMIT, window, or compaction** and replays them every turn. A long-lived conversation grows context every turn → rising latency/cost and eventual context rot (distraction/pollution per the Breunig taxonomy). Maps to: *monitor tokens and compact BEFORE the rot zone.*
**Fix (Phase 3):** a **boundary-safe** recency window — keep the most recent N messages, but only start the retained window at a clean user turn so a `tool_use` is never severed from its `tool_result` (which would 400 the API). Short/normal chats are unaffected.

### D2 — noted, low risk — analysis inputs are pre-truncated.
`llm_judge` slices transcript to 24 000 chars / insights to 12 000 (`llm_judge.py:252-257`); `ai_analysis` builds fresh context per call (no cross-call accumulation). These are single-shot, so no rot. Recorded for completeness.

---

## (e) Eval / observability gaps

### E1 — P1 — Model+version selection is not centralized, so it isn't uniformly observable.
Because each site picks its own model string, cost/latency **cannot be sliced by (model, version) uniformly**: `record_llm_completion` and `LLM_LATENCY` labels are added by hand per call-site (`entity_resolution.py:471`, `scorecard_service.py:130`, `warnings_commitments.py:562,682`, `ai_analysis.py:1189`, `email_reply.py:375`, …) and several sites (KB family, `text_segmenter`, `llm_judge`) don't record telemetry at all. Centralizing ids (B1) is the prerequisite; funnelling calls through the seam (recommendation) is the full fix.

### E2 — P2 — Failovers/rate-limits aren't logged (because there's no failover). Addressed by B2's logging.

### Strengths (keep): grounded judge → `insight_quality_scores`, `regression_watchdog`, learned-ceiling telemetry flywheel (`llm_telemetry.recompute_ceilings`), Prometheus histograms.

---

## (f) Layer B — build-time (Claude Code) gaps

- **F1 — P1 — No project-level subagents.** `.claude/` contains only `settings.local.json` (permission allowlist). There are **no committed `.claude/agents/`**, so every dev session runs on the main (heavy) model even for read-only search or routine edits — no cheap-tier default. Fixed in Phase 4 (Haiku explore + Sonnet implement subagents; Opus reserved for hard design).
- **F2 — P2 — No documented dev-time model convention.** No repo `CLAUDE.md`. Phase 4 adds a "Model routing (dev)" section. (Advisory ~70%; the subagent `model:` assignments are the real enforcement.)
- **Fable 5 must stay out of Layer A** (it is — no app code references it except the `_NO_SAMPLING_PARAM_MODELS` guard set, which is correct) **and is never a default in Layer B** (capacity-constrained; ~2× an Opus call). Phase 4 sets no subagent to Fable.

---

## Fix plan (what Phase 3/4 commit vs. defer)

**Committed (Layer A, Phase 3):**
1. `model_catalog.py` — single source of truth; tier→id resolved from env-overridable `config.py` settings (defaults pinned). Repoint all constants in B1 to it. *(fixes B1, H1-partial, E1-prereq)*
2. `safe_create()` bounded retry + optional model failover on transient errors, wired into the router path, with logged reasons. *(fixes B2, E2)*
3. Boundary-safe history window in `_load_history`. *(fixes D1)*
Each with tests written first (red → green), including a **guard test** that greps the service tree for stray hardcoded `claude-*` literals so B1 can't silently regress.

**Committed (Layer B, Phase 4):** project subagents + `CLAUDE.md` dev-routing section.

**Deferred (recommendations, not committed — risk/ownership):**
- [x] **DONE (follow-up PR).** Migrate the ~24 direct-call touchpoints through `ModelRouter`. Every runtime `messages.create`/`stream` now routes through the router (`ainvoke`/`invoke`/`astream`), so all touchpoints inherit tier-pinning, transient-retry + failover, and uniform telemetry. The router gained `forced_tier`, `messages`/`tools`/`timeout` passthrough, an `astream` streaming path, and folded-in `record_llm_completion`. Temperature was set per task (0.0 structured, ~0.3 prose, 0.7 Linda chat). **This subsumes the telemetry item below.**
- [x] **DONE (via the migration).** Uniform `record_llm_completion` — the router now records every routed call via `call_site`; the KB family / `text_segmenter` / `llm_judge` are covered.
- [x] **DONE (PR #146).** Bump Sonnet `4-6 → 5` (B4). Not a one-liner after all — the current model docs show Sonnet 5 (a) 400s on non-default sampling params and (b) defaults adaptive thinking on. Fixed centrally: `claude-sonnet-5` added to `NO_SAMPLING_PARAM_MODELS` (temperature omitted) and the router sends `thinking={"type":"disabled"}` for default-on models (with a new `LLMRequest.thinking` opt-in). **Staging-trial watch:** Sonnet 5's new tokenizer emits ~30% more tokens for the same text — watch truncation (`stop_reason=max_tokens`) on tight Sonnet caps (Ask Linda 2048, email reply 2048) and re-baseline cost; the per-task temperatures no longer apply to Sonnet-tier calls (steered via prompt instead).
- [x] **DONE (PR #146).** DB default model string + Alembic migration (`as01f5b7c9d0`) → `claude-sonnet-5`; single head preserved.
- [x] **DONE (PR #146).** Post-failover `.tier` reconcile — `model_catalog.tier_for_model()` + router now report the served tier (and telemetry tier), not the originally-selected one.
- [ ] Extend failover to the Batches path (`model_router.submit_batch`) — left out because batch is non-interactive (retried out-of-band), so the live path was prioritized. Only remaining item.
