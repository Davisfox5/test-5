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
The reuse check skips the expensive Sonnet analysis only if a prior summary already exists.
Two concurrent retries can both observe the pre-analysis state and both pay for the call.
No "synthesis started" marker, no atomic state transition, no idempotency key.
- Evidence: `backend/app/tasks.py:1220–1240` (reuse check), `:1259–1262` (reuse log),
  `:2259–2602` (retry logic, `max_retries=3`).

### 3b. Event-loop × connection-pool coupling (crash risk)
The async engine's asyncpg pool binds connections to the loop that created them. After a
beat job's loop closes, the next `asyncio.run()` on the same prefork child can raise
*"Event loop is closed"* / *"Future attached to a different loop."* Dispose-first + TCP
keepalive mitigate within a task but not across tasks.
- Evidence: `backend/app/tasks.py:697–704` (failure explanation), `:590–607` (keepalive),
  `:713`, `:1786–1796` (dispose-first pattern). Pool config in `backend/app/db.py`.

### 3c. Orphaned interactions / no recovery path (correctness risk)
Entity resolution is best-effort and depends on *fresh* insights. If analysis (step 9)
succeeded but resolution (step 12b) failed, a retry must not re-run the paid analysis — so
it resolves against stale insights and produces inconsistent customer linkage. The only
idempotency key is `interaction_id`.
- Evidence: `backend/app/tasks.py:1440–1475` (entity resolution best-effort),
  `:764` (idempotency claim on Contact upsert). `backend/app/services/entity_resolution.py`
  (35 TODO/race/idempotency markers).

### 3d. Opaque orchestration failures (observability + elasticity risk)
Per-tenant work fans out via Celery chords with a 1-hour timeout; one slow tenant times out
the whole chord and reports "0 processed" with no partial-success path. Heavy scans run all
tenants sequentially under one shared sync session, so one slow tenant blocks the rest.
- Evidence: `backend/app/tasks.py:3319–3336` (daily chord), `:3464–3472` (weekly chord),
  `:3072–3084` (sequential `support_trend_scan`), `:3111–3119` (sequential `cohort_scan`).

## 4. Dependency structure (what unblocks what)

- **3a** and **3c** both dissolve if each interaction carries **durable per-step status +
  an idempotency key**, so retries *resume* instead of *re-run*. This is the foundational fix.
- **3b** is largely orthogonal (infra/runtime); it can be stabilized independently and
  arguably first, because it's an acute crash.
- **3d** builds on the same durable-state idea (a chord needs per-entity resumable state to
  offer partial success) but is a layer up.

## 5. Ground truth we still need before designing

- [ ] Precise map of the current implicit state machine: every step, what it writes, which
      writes are idempotent, where money is spent, and where the transaction boundaries are.
- [ ] Production signal: how often do 3a/3b/3c/3d actually fire? (telemetry, Sentry, logs)
- [ ] What durable-state primitives already exist? (`WriteProposal` staging, any
      `status`/`processing_state` columns on `Interaction`, dead-letter table)
- [ ] Migration appetite / constraints given the fragile chain (see challenge #4).

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
