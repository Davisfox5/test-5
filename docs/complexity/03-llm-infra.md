# 03 — Model-agnostic LLM infra: cost, quality & context-rot governance

**Status:** 🔵 In implementation (fix built + tested on `claude/llm-infra-hardening`; merge pending approval)
**Owner:** _tbd_ · **Working doc — evolves as we design.**

> Companion to the [agent-infra audit](../agent-infra-audit.md) (2026-07-01), which
> planned and largely shipped this challenge's fixes (PRs #144–#146 + follow-ups).
> This doc is the **verification pass**: which audit items are genuinely closed
> (with evidence), which regressed or were never finished, and the residual gaps
> that remain open. Verified against `claude/llm-infra-hardening` off `main`
> @ 2026-07-05.

---

## 1. The problem in one sentence

Every runtime LLM decision — which model id, which tier, what token budget, what
happens on provider failure, and how much conversation history gets replayed — must
live behind one seam so a model deprecation, a tokenizer change, or a long-lived
chat cannot silently break or bloat production; the audit built that seam, and this
doc verifies it holds.

## 2. Verification results — audit items CONFIRMED CLOSED

Each item re-verified by grepping the full runtime tree and reading the seam code,
not by trusting the audit's checkboxes.

| Audit item | Verdict | Evidence |
|---|---|---|
| **B1** — single source of truth for model ids | ✅ Closed | `backend/app/config.py:75-77` (env-overridable ids), `backend/app/services/model_catalog.py` (tier resolution, capability sets, failover map). Full-repo sweep found **zero** stray `claude-*` literals in runtime code; remaining literals are config defaults, catalog capability sets, alembic/seed SQL defaults, tests, and docs. Guard test passes (`tests/test_model_catalog.py:98-119`). |
| **H1** — routing seam actually used | ✅ Closed | All ~30 runtime touchpoints (24 files) call `ModelRouter.ainvoke/invoke/astream/run_batch`; the only `messages.create`/`messages.stream`/`batches.*` calls live in the sanctioned seams: `llm_client.py:191` (failover wrapper) and `model_router.py:367,423,661,675`. No `anthropic.AsyncAnthropic(...)` construction outside `llm_client.py`. |
| **B2** — transient retry + model failover | ✅ Closed | `acreate_with_failover` (`llm_client.py:163-221`): bounded exponential backoff on transient (429/5xx/timeout/connection), one down-tier failover on model-unavailable (404) or retry exhaustion, every retry/failover logged with reason. Non-transient 4xx re-raise. `tests/test_llm_failover.py` covers it. |
| **Post-failover tier reconcile** | ✅ Closed | `model_router.py:250-256` — served model → `model_catalog.tier_for_model()` → `LLMResponse.tier` and telemetry report the tier that actually served the request. |
| **Batches-path failover** | ✅ Closed | `run_batch` (`model_router.py:469-601`): submit with bounded transient retry (`_create_batch:409-437`), per-entry one-step-down failover for retryable errors (`llm_client.batch_error_is_retryable:149-160`), every submitted `custom_id` present in results (`result_unavailable` for poll-timeout, deliberately non-retryable, `model_router.py:531-541`), best-effort second round (first-round results never discarded, `:583-591`), sequential-`ainvoke` fallback when SDK lacks the surface. `tests/test_batch_failover.py` covers it. |
| **Batch entries share live request shaping** | ✅ Closed | `_batch_entry` (`model_router.py:399-407`) reuses `_build_create_kwargs` → batch calls inherit the temperature guard and Sonnet-5 thinking suppression. |
| **Sonnet-5 bump gotchas (PR #146)** | ✅ Closed | `claude-sonnet-5` in `NO_SAMPLING_PARAM_MODELS` (`model_catalog.py:87-97`); thinking suppressed when either the primary **or the failover** model defaults it on (`model_router.py:297-307` — an Opus→Sonnet-5 failover can't silently re-enable adaptive thinking). |
| **Fable never called from app code** | ✅ Closed | `fable` appears only in comments and the `NO_SAMPLING_PARAM_MODELS` capability set; no call path resolves to it. |
| **Uniform telemetry via `call_site`** | ✅ Closed for `ainvoke`/`invoke`/`run_batch` | Router records every routed completion (`model_router.py:316-328`); successful batch entries recorded via `_record_batch_entry` (`:603-613`). `llm_call_telemetry` captures model, tier, tokens, cache counts, `stop_reason`, `truncated`; `LLM_TRUNCATIONS` Prometheus counter + nightly `recompute_ceilings` watches truncation-rate > 5% (`llm_telemetry.py:345-427`). **Exception: the streaming path — see gap G2.** |
| **D1** — bounded chat history window | ◐ Partially closed | `_window_history` (`linda_agent.py:417-438`, `HISTORY_WINDOW_MESSAGES = 40` at `:55`) is implemented, boundary-safe for its inputs, and tested (`tests/test_linda_history_window.py`). **But replay itself is broken upstream of the window — see gap G1.** |
| **Batches adoption (nightly rollup)** | ✅ Closed | `orchestrator.py:395` submits entity consolidation through `run_batch`. Remaining `invoke` surfaces (weekly reflection, manager recs, KB refiners) were explicitly left open as low-value batch candidates — a cost note, not a regression. |

## 3. Open gaps (the residual work)

### G1 — P0 — Linda chat history replay produces API-invalid message sequences after any tool-using turn.

`_load_history` (`linda_agent.py:441-457`) replays only rows with
`role in ("user", "assistant")`. But a tool exchange is persisted as **three** rows:
the assistant row whose `tool_calls` JSON **includes the `tool_use` blocks**
(`linda_agent.py:541-549`), then the tool results as a row with **`role="tool"`**
(`linda_agent.py:582-590`). On the next turn, the `role="tool"` row is silently
dropped, so the replayed history contains an assistant `tool_use` message with **no
following `tool_result`** — which the Anthropic API rejects with a 400
(`tool_use` ids without `tool_result` blocks immediately after).

Net effect: **an Ask Linda conversation permanently 400s on every turn after the
first turn that used a tool.** Present since the original Ask Linda commit
(`8fcd05e`); pre-dates the audit, which looked at the window's boundary logic but
not the replay's row filter. The `_is_tool_result_content` guard in
`_window_history` is dead code in practice — replayed histories can never contain a
tool_result message, only dangling tool_use.

The existing tests miss it because they exercise `_window_history` on synthetic
in-memory messages, never `_load_history` on persisted rows.

### G2 — P1 — The streaming path records no telemetry: Ask Linda is observability-dark.

`astream` (`model_router.py:337-388`) yields the stream and never calls
`self._record()`, and the caller discards the final message's usage
(`linda_agent.py:528`). So `call_site="linda_chat"` — the highest-visibility,
customer-facing surface — produces **no `llm_call_telemetry` rows, no
`LLM_TRUNCATIONS` counts, and no learned-ceiling data**. A truncation regression on
Ask Linda would be invisible to the nightly watchdog.

### G3 — P1 — Tight fixed 2048 caps on Sonnet-5 surfaces, with no truncation handling.

Sonnet 5's tokenizer emits ~30% more tokens for the same text (audit line 129's
staging-trial watch item — never followed up with a guard):

- **Ask Linda:** `MAX_TOKENS = 2048` (`linda_agent.py:47`), fixed, not routed
  through `compute_max_tokens`, no learned ceiling. On `stop_reason="max_tokens"`
  the tool loop just treats it as end-of-turn (`linda_agent.py:552`) — the user
  gets a silently cut-off answer, unlogged (and unmeasured, per G2).
- **Email reply:** `max_tokens=2048` (`email_reply.py:380`), no `stop_reason`
  check. A truncated response fails JSON parsing and ships the raw truncated text
  as the draft body with `requires_human_review=True` (`email_reply.py:389-399`) —
  graceful-ish, but a degraded customer-facing artifact with no truncation-specific
  signal.

Contrast: `ai_analysis` has the model behavior — detect `max_tokens`, retry once
with doubled budget, stamp diagnostics (`ai_analysis.py:1230-1276`).

### G4 — P2 — Stray-literal guard test scans too narrow a tree.

`test_no_hardcoded_model_ids_in_services_or_api` scans only
`backend/app/services` + `backend/app/api` (`tests/test_model_catalog.py:102-107`).
`backend/app/*.py` (`tasks.py`, `main.py`, `config.py`), `backend/scripts/`, and
`scripts/` are unguarded — currently clean (verified by sweep), but a stray literal
added to `tasks.py` tomorrow would pass CI. (`config.py:75-77` and the catalog's
capability sets are the legitimate allowlist entries; `backend/scripts/seed_sales.py:118`
carries a SQL default that needs an explicit allow-or-fix decision.)

### G5 — P2 — Learned ceilings can only ever shrink budgets on fixed-cap surfaces.

`compute_max_tokens` clamps learned ceilings to the static tier cap
(`llm_client.py:286-298`) — fine — but the two 2048-cap surfaces above don't call
it at all, so the telemetry flywheel (observe → recompute → apply) has no way to
relieve a too-tight cap even where the data says it truncates. Folding G3's fix
through `compute_max_tokens(call_site=...)` closes this for free.

## 4. Discrepancies vs. the audit (summary for review)

1. **Audit said D1 "DONE"** — the window is done; the replay underneath it was
   already broken for tool-using conversations (G1). The audit's checkbox was
   honest about what it built but verified the wrong layer.
2. **Audit said telemetry "covers every routed call"** — true for
   `ainvoke`/`invoke`/`run_batch`, false for `astream` (G2), which is exactly the
   Ask Linda surface the Sonnet-5 watch item worried about.
3. **Audit's Sonnet-5 "staging-trial watch" (truncation on tight caps) was a watch
   item with no watcher** — the surfaces it named can't observe truncation (G2) and
   don't handle it (G3).
4. Everything else the audit marked done is genuinely done, with evidence above.

## 5. Open design questions (to decide together)

1. **G1 fix shape:** replay `role="tool"` rows as `user`/tool_result messages
   (faithful replay), or strip the dangling `tool_use` blocks from assistant
   messages at replay time (lossy but simpler)? Faithful replay preserves the
   model's memory of tool outputs across turns; stripping loses it but can't
   resurrect stale tool data.
2. **G1 data repair:** existing conversations already contain the three-row shape —
   replay fix alone heals them (no migration needed) if we choose faithful replay.
   Confirm no backfill required.
3. **G2 fix shape:** record telemetry inside `astream` (needs the final message,
   which only the caller sees) vs. a small `router.record_stream_completion(req,
   final_message)` the caller invokes after `get_final_message()`. The latter is
   honest about who owns the final message; the former needs a wrapper around the
   stream object.
4. **G3 truncation guard:** raise the caps (max_tokens is an upper bound — unused
   budget costs nothing), add retry-on-truncation (like `ai_analysis`), or both?
   Streaming can't transparently retry (tokens already reached the user), which
   argues for a higher cap + logged detection on Ask Linda, and cap+retry on email
   reply.
5. **G4 scope:** extend the guard test to `backend/` + `scripts/` with an explicit
   allowlist (config defaults, catalog, alembic, seeds, tests) — any reason not to?

## 6. Chosen approach (decided 2026-07-05)

1. **G1 — faithful replay.** `role="tool"` rows replay as user/`tool_result`
   messages, restoring the exact wire shape (Linda keeps memory of prior tool
   outputs; broken conversations heal with no data migration). A pairing
   repair pass additionally strips unpaired `tool_use`/`tool_result` blocks
   left by turns that crashed mid-exchange, so one bad turn can't poison the
   conversation.
2. **G2 — caller records the final message.** New
   `ModelRouter.record_stream_completion(req, final_message)` with the same
   served-tier reconciliation as `ainvoke`; `run_chat_turn` calls it after
   `get_final_message()`.
3. **G3 — raise caps + detect/log.** Both surfaces go to a 4096 override
   routed through `compute_max_tokens(call_site=...)` (learned ceilings now
   apply). Truncation is logged on Ask Linda (streaming can't safely retry)
   and forces `requires_human_review` on email reply even when the cut-off
   JSON parses.
4. **G4 — extend the guard and fix the seed.** The stray-literal guard scans
   all of `backend/` + `scripts/` (allowlist: `model_catalog.py`, `config.py`,
   frozen alembic versions); `seed_sales.py`'s DB default now interpolates
   `model_catalog.SONNET`.

## 7. Implementation increments

All landed red-first on `claude/llm-infra-hardening`; suite green
(1438 passed / 7 skipped), app-import smoke check clean.

1. **G1 + G2 + G3** — `linda_agent.py` (`_rows_to_messages`,
   `_repair_tool_pairing`, computed cap, truncation log, stream-telemetry
   call), `model_router.py` (`record_stream_completion`), `email_reply.py`
   (`_draft_from_response`, computed cap). Tests:
   `tests/test_llm_infra_hardening.py` (wire-validity property test over
   windowed tool conversations; served-tier reconciliation; truncation
   flagging; full fake-router turn integration).
2. **G4** — guard-test scope extension
   (`tests/test_model_catalog.py::test_no_hardcoded_model_ids_in_runtime_tree`)
   + `backend/scripts/seed_sales.py` catalog interpolation.
