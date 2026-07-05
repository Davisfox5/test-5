# 01 — Exactly-once correctness in the async LLM pipeline

**Status:** 🟡 Discussing
**Owner:** _tbd_ · **Working doc — evolves as we design.**

---

## 1. The problem in one sentence

The core interaction pipeline is an implicit, ~17-step state machine whose steps are
non-idempotent, cost real money, and mix sync-Celery with async code — so a failure or
retry partway through can double-charge an LLM call, orphan an interaction, or crash on a
stale event loop, with no durable record of what already succeeded.

## 2. Why this is hard (not just buggy)

This is the classic distributed-systems problem: **exactly-once effects over a stateful,
multi-step, side-effecting workflow.** You cannot get exactly-once delivery, so you engineer
*exactly-once effects* via idempotency + durable step state + reconciliation. Today the
pipeline has almost none of that scaffolding; correctness rests on "the happy path usually
happens, and errors are logged and swallowed."

## 3. Decomposition — four interacting sub-problems

### 3a. Idempotency hole in the analysis retry (money risk)
The reuse check skips the expensive Sonnet analysis only if a prior summary already exists
(`len(summary) >= 40` and no `insights["error"]` stamp). Verified 2026-07-05 — it is worse
than first written up, in two ways:

1. **The guard almost never fires on its stated path (retry-after-failure).** The analysis
   output is first durably committed only at step 14a (`tasks.py:1775`) — any failure in
   steps 9b–14 rolls the session back and loses the paid output, so the retry re-pays no
   matter what the guard says. And when the prior attempt *did* commit (failure in steps
   15+), the failure handler merges an `error` stamp into `insights`
   (`tasks.py:2472–2481`), which the guard treats as "don't reuse" (`:1230–1234`) — so the
   retry re-pays *even though a good, persisted analysis exists*.
2. **Concurrent duplicates race past it.** `task_acks_late=True` +
   `visibility_timeout=3600` (`tasks.py:83`, `:109`) means a pipeline run longer than 1h is
   redelivered while still running; API endpoints can also double-enqueue the same
   interaction (`api/interactions.py:401,583,727`, `api/admin.py:1268`). The guard is
   read-then-act on row content — no atomic claim, no "analysis started" transition, no
   idempotency key — so both executions pay Sonnet.

- Evidence: `backend/app/tasks.py:1220–1240` (reuse check), `:1236–1239` (reuse log),
  `:1775` (first commit of insights, step 14a), `:2140` (final commit),
  `:2457–2485` / `:2576–2600` (rollback + error stamp + `self.retry`, `max_retries=3`),
  `:83`/`:109` (acks_late + visibility timeout).

### 3b. Event-loop × connection-pool coupling (crash risk)
The async engine's asyncpg pool binds connections to the loop that created them. After a
beat job's loop closes, the next `asyncio.run()` on the same prefork child can raise
*"Event loop is closed"* / *"Future attached to a different loop."*

Correction (verified 2026-07-05): the dispose-first pattern in `_run_async`
(`tasks.py:689–716`) **is** the cross-task mitigation, and most beat tasks now use it. The
*residual* exposure is that the pattern is a per-call-site convention, not a structural
guarantee:
- Async-engine call sites inside the pipeline's `_TaskEventLoop` that **don't** dispose
  first — e.g. `_emit_lifecycle` (`tasks.py:1327–1343`) checks out `async_session` with no
  dispose; a stale-loop connection there fails silently (swallowed → lost lifecycle
  webhooks). Plan synthesis does dispose inline (`:1795–1796`); lifecycle emit doesn't.
- Any new raw `asyncio.run()` site silently reintroduces the bug (e.g. `:2358`, `:2390`,
  `:2668`, `:2740`, `:2998`, `:3086` are raw — today they're HTTP-only/sync-session paths,
  but nothing stops async-engine use creeping in).
- `engine.dispose()` is a per-process global on a shared engine — safe under prefork
  (one task at a time per child), a footgun if the worker pool model ever changes.
- Evidence: `backend/app/tasks.py:689–716` (`_run_async` + failure explanation),
  `:590–607` (keepalive), `:713`, `:1786–1796` (dispose-first). Pool config in
  `backend/app/db.py` (async 15+5; Celery sync engine 5+5 at `tasks.py:612–623`).

### 3c. Orphaned interactions / no recovery path (correctness risk)
Entity resolution (step 12b) is best-effort: any exception is logged and swallowed
(`tasks.py:1471–1475`), the interaction still lands `status="analyzed"`, and the inline
comment says the orphan "can be reprocessed later" (`:1438`) — **but no reconciliation
job exists**; nothing ever finds or re-resolves orphans, so a partial failure leaves the
row permanently unlinked (no `customer_id`/`contact_id`, excluded from customer rollups,
briefs, and lifecycle events). Because resolution runs *before* the first commit
(step 12b < step 14a's commit), a swallowed resolution failure also can't be
distinguished afterwards from "there was genuinely nobody to resolve."

Corrections to the original write-up (verified 2026-07-05):
- The "resolves against stale insights on retry" framing was muddled — when the reuse
  guard fires, resolution re-runs with the *same persisted* insights, which is fine. The
  real gap is the missing recovery path above.
- The `:764` citation pointed at `tasks.py` (that line is audio-staging cleanup); the
  intended cite is `backend/app/services/entity_resolution.py:738`/`:764`
  (owner-link upsert documented idempotent).
- "35 TODO/race/idempotency markers" was a grep artifact (`race` matched `trace`); the
  real count is 3, and resolution already holds a pg advisory xact lock around customer
  dedupe (`entity_resolution.py:295–305`) — more race protection than implied.
- Evidence: `backend/app/tasks.py:1434–1475`, `backend/app/services/entity_resolution.py`.

### 3d. Opaque orchestration failures (observability + elasticity risk)
Per-tenant work fans out via Celery chords, but the dispatcher then **blocks inside a
Celery task** on `async_result.get(disable_sync_subtasks=False, timeout=3600)` — a
sync-wait-in-task anti-pattern that pins a worker slot for up to 1h and can deadlock a
saturated pool. On timeout it reports `tenants_processed: 0` and marks *every* tenant
failed, even though the per-tenant subtasks may all have completed — the work isn't lost,
the *report* lies, and there is no partial-success path. Heavy scans
(`support_trend_scan`, `cohort_recommendation_scan`) still run tenants sequentially under
one shared sync session (deliberately — the session isn't safe for concurrent use), so one
slow tenant delays the rest.
- Evidence: `backend/app/tasks.py:3319–3336` (daily chord + blocking `.get`),
  `:3464–3472` (weekly chord), `:3072–3084` (sequential `support_trend_scan`),
  `:3111–3119` (sequential `cohort_scan`).

## 4. Dependency structure (what unblocks what)

- **3a** and **3c** both dissolve if each interaction carries **durable per-step status +
  an idempotency key**, so retries *resume* instead of *re-run*. This is the foundational fix.
- **3b** is largely orthogonal (infra/runtime); it can be stabilized independently and
  arguably first, because it's an acute crash.
- **3d** builds on the same durable-state idea (a chord needs per-entity resumable state to
  offer partial success) but is a layer up.

## 5. Ground truth (filled in 2026-07-05)

- [x] **Step map + transaction boundaries.** `_run_pipeline_impl` (`tasks.py:993`) runs
      steps 5→19 in one sync session with exactly three commits:
      | Commit | Where | What becomes durable |
      |---|---|---|
      | `:1212` | pre-analysis (step 9 entry) | reads only; releases conn before the 30–90s Sonnet call |
      | `:1775` | step 14a (plan synthesis) | **first durable point for `insights` + `status="analyzed"`** — also action items, scores from steps 9b–14 |
      | `:2140` | end of pipeline | scores/snippets/features/webhook rows (steps 15–19) |
      Money is spent at step 7.5 (Haiku segmenter, text path), step 8 (triage), step 9
      (Sonnet analysis — the big one), step 10 (batched Haiku scorecards), step 14a
      (plan synthesis), plus Deepgram at steps 3–4. Everything between commits re-runs on
      retry.
- [x] **Production signal:** product is pre-launch — zero tenants, no traffic, so there is
      no telemetry to mine. Instrument-and-measure-first yields ~nothing; the acute fixes
      are cheap insurance instead.
- [x] **Existing durable primitives:** `Interaction.status`
      (`processing`/`analyzed`/`failed`/`transcription_failed` — plain String, no CHECK,
      no claim semantics), `insights` JSONB (error stamps, diag blocks), unique
      `message_id` on email interactions, `WebhookDelivery` rows with retry status, pg
      advisory xact lock inside entity resolution. **No** per-step ledger, no
      `WriteProposal` staging for the pipeline, no dead-letter table for pipeline steps
      (there is a task-failure log signal at `tasks.py:504–523`).
- [ ] Migration appetite / constraints given the fragile chain (see challenge #4) —
      note: two-heads risk means any migration here must check `alembic heads` right
      before merge.

## 6. Open design questions (to decide together)

1. **Sequence:** stop-the-bleeding first (3b crash + 3a double-charge as targeted patches),
   or go straight to the foundational durable state machine that subsumes 3a/3c?
2. **State model:** a `processing_state` enum + step bitmap/columns on `Interaction`, vs. a
   separate `interaction_step_run` ledger table (one row per step attempt)?
3. **Idempotency-key granularity:** per-interaction, per-(interaction, step), or per-
   (interaction, step, input-hash) so re-analysis after input change is allowed but retries
   are deduped?
4. **Runtime fix for 3b:** per-task engine/loop lifecycle, a NullPool for Celery, or a
   sync driver for the Celery path — what's the least-risk change?
5. **Framework question:** stay hand-rolled on Celery, or adopt a durable-execution pattern
   (idempotent tasks + explicit saga) — without over-engineering?

## 7. Chosen approach

_To be filled in once we've talked through §6._

## 8. Implementation increments

_To be sequenced once §7 is agreed. Each increment: red tests first → implement → verify._
