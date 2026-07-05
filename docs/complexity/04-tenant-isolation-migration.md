# 04 — Multi-tenant isolation + mid-flight action-model migration

**Status:** 🟡 Discussing
**Owner:** _tbd_ · **Working doc — evolves as we design.**

> **Pre-launch lens (2026-07).** Zero tenants, zero production data. This is the *cheapest
> possible moment* to fix both halves of this challenge: an RLS backstop needs no data
> migration, and the action-model cutover has nothing to migrate. Both get dramatically
> more expensive after launch.

---

## 1. The problem in one sentence

Tenant isolation — the core guarantee of a white-label B2B product — is enforced only by
**213 hand-written `.tenant_id == …` filters across 44 API files**, with no runtime backstop,
so a single forgotten filter in any current or future endpoint silently leaks one customer's
data to another; and separately, the action model is mid-cutover with **non-atomic dual writes**
that can leave records half-migrated.

## 2. Why this is hard (and why it's the worst blast radius of the five)

Isolation-by-discipline is a losing game asymptotically: correctness depends on every developer
remembering, on every query, forever, across a growing surface. The failure is silent (no error,
no crash — just wrong rows returned) and the consequence in a white-label product is
deal-ending. There's no partial credit: 212 correct filters and one miss is a breach.

## 3. Sub-problem 4a — isolation enforcement gap (the star)

### Current state (evidence)
- Auth/authz **is** centralized: `auth.py` `get_current_principal` resolves the tenant;
  `require_role` / `require_scope` gate access. This part is strong.
- But **row-level data scoping is manual**: the dep hands you `principal.tenant.id`; each query
  must filter on it by hand. **213 occurrences of `.tenant_id ==` across 44 files**
  (`api/contacts.py` 29, `api/manager.py` 14, `api/admin.py` 12, `api/action_plans.py` 9, …).
- **No runtime backstop:** no Postgres RLS, no SQLAlchemy `with_loader_criteria`/global filter.
  Verified by grep — nothing enforces tenant scope if a query omits the filter.

### The legitimately-global tables (must be exempt from any blanket rule)
- `PromptVariant` (`models.py:2491`) — versioned prompts, cross-tenant by design.
- `CrossTenantAnalytic` (`models.py:2599`) — aggregate metrics, "no tenant_id by design."
- `EvaluationReferenceSet` (`models.py:2548`) — `tenant_id` nullable; global reference sets.
- Also: the synthetic **API-key admin principal** and any reseller/admin cross-tenant surface.

### Enforcement options (defense-in-depth menu)
- **RLS (Postgres row-level security).** Per-request `SET LOCAL app.current_tenant`; policies on
  every tenant-scoped table. **Fails closed at the DB regardless of app bugs** — the only option
  that survives a forgotten filter. Cost: set the tenant GUC per transaction on *both* the async
  API engine and the sync Celery engine (interacts with the #1 pool/loop issues), policies +
  exemptions for the global tables, and a superuser/bypass path for migrations & admin.
- **App-level global filter.** A context-var tenant + `with_loader_criteria` injected on every
  ORM query. No DB change; good ergonomics. But bypassable by raw `session.execute(text(...))`
  and must handle both engines + global-table exemptions.
- **Repository/scoped-session pattern.** All reads go through a helper that requires a tenant.
  Explicit, but doesn't stop a developer from going around it.
- **Test/lint guard.** A test that enumerates tenant-scoped tables/endpoints and asserts scoping;
  catches regressions early but is not a runtime backstop.

These compose. The strong posture is **RLS as the runtime backstop + a test guard to catch gaps
in review**, optionally an app-level filter for ergonomics on top.

## 4. Sub-problem 4b — action-model dual-write cutover

Legacy `ActionItem` (flat ~79-col list) and the new `ActionPlan → ActionStep →
StepArtifact/StepResponse` DAG both write on every interaction. Plan synthesis runs in a
*separate* AsyncSession with an independent commit outside the sync transaction and "never blocks
the pipeline — on any error we log and continue" (`tasks.py:1737–1869`, esp. `:1742`). So an
`ActionItem` can exist with no `ActionPlan`: not atomic, dual-read schema skew for clients, no
deprecation timeline. **Pre-launch, with nothing to migrate, we can simply finish the cutover:**
pick the DAG as canonical, stop dual-writing, retire the legacy path.

## 5. The pre-launch advantage (why now)

- **RLS now = free.** Zero rows to backfill, zero tenants to break, no live traffic. Post-launch,
  introducing RLS means auditing 89 tables against real data and risking a lockout incident.
- **Action-model cutover now = free.** No production `ActionItem` rows to migrate to the DAG.
- Both interact with the **fragile migration chain** (challenge #4-adjacent, see the `ai_insights`
  incident) — doing them while the chain is short and data-free is far safer.

## 6. Recommended first step

A **one-table RLS spike** to de-risk the approach before committing to all 89 tables: enable RLS
on `Interaction` end-to-end — set `app.current_tenant` in the API request middleware *and* in the
Celery task context, add the policy, and prove with a test that a cross-tenant read returns zero
rows through both the async and sync engines. This answers the single make-or-break question
(does the per-connection tenant GUC work cleanly with our pooling + sync/async split?) cheaply.
Pair it with a **scoping test guard** that inventories tenant-scoped tables so we can measure the
gap and prevent regressions immediately.

## 7. Open design questions

1. **Backstop choice:** RLS (fails closed at DB) vs. app-level `with_loader_criteria` filter
   (no DB change, bypassable)? My lean is RLS *because* it's a security guarantee and now is the
   cheapest it will ever be.
2. **GUC plumbing:** where do we set the tenant on each connection — API middleware + a Celery
   task wrapper? How does it coexist with the #1 pool/loop lifecycle?
3. **Global-table policy:** explicit allow-list of tenant-less tables + a documented rule for
   future ones (a new-table checklist / test).
4. **Migration/admin bypass:** how do Alembic migrations and legitimate cross-tenant admin/reseller
   surfaces run without tripping RLS (a `BYPASSRLS` role or a scoped exemption)?
5. **Sequence vs. 4b:** do the isolation backstop first (security), then the action-model cutover;
   or interleave since both want migrations while the chain is short?

## 8. Chosen approach

_To be filled in once we've talked through §6–§7._

## 9. Implementation increments

_To be sequenced once §8 is agreed. Each increment: red tests first → implement → verify._
