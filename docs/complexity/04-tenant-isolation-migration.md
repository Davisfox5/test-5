# 04 — Multi-tenant isolation + mid-flight action-model migration

**Status:** 🟢 Plan agreed (isolation) · 🟡 Discussing (action-model cutover)
**Owner:** _tbd_ · **Working doc — evolves as we design.**

> **Pre-launch lens (2026-07).** Zero tenants, zero production data. This is the *cheapest
> possible moment* to fix both halves of this challenge: an RLS backstop needs no data
> migration, and the action-model cutover has nothing to migrate. Both get dramatically
> more expensive after launch.

---

## 1. The problem in one sentence

Tenant isolation — the core guarantee of a white-label B2B product — is enforced only by
**213 hand-written `.tenant_id == …` filters across 44 API files**, with no runtime backstop,
and the same logical-filter-only pattern repeats in the vector store; a single forgotten filter
in any current or future code path silently leaks one customer's data to another. Separately,
the action model is mid-cutover with **non-atomic dual writes** that can leave records
half-migrated.

## 2. Why this is hard (and why it's the worst blast radius of the five)

Isolation-by-discipline is a losing game asymptotically: correctness depends on every developer
remembering, on every query, forever, across a growing surface. The failure is silent (no error,
no crash — just wrong rows returned) and the consequence in a white-label product is
deal-ending. There's no partial credit: 212 correct filters and one miss is a breach.

## 3. Sub-problem 4a — isolation enforcement gap in Postgres (the star)

### Current state (evidence)
- Auth/authz **is** centralized: `auth.py` `get_current_principal` resolves the tenant;
  `require_role` / `require_scope` gate access. This part is strong.
- But **row-level data scoping is manual**: the dep hands you `principal.tenant.id`; each query
  must filter on it by hand. **213 occurrences of `.tenant_id ==` across 44 files**
  (`api/contacts.py` 29, `api/manager.py` 14, `api/admin.py` 12, `api/action_plans.py` 9, …).
- **No runtime backstop:** no Postgres RLS, no SQLAlchemy `with_loader_criteria`/global filter.
  Verified by grep — nothing enforces tenant scope if a query omits the filter.
- **The schema is already RLS-ready:** `tenant_id` is present and consistent across ~84
  tenant-scoped tables via CASCADE FKs, so adding policies is mechanical rather than a redesign.

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

These compose. The chosen posture (see §8) is **RLS as the runtime backstop + a test guard**.

## 4. Sub-problem 4b — action-model dual-write cutover

Legacy `ActionItem` (flat ~79-col list) and the new `ActionPlan → ActionStep →
StepArtifact/StepResponse` DAG both write on every interaction. Plan synthesis runs in a
*separate* AsyncSession with an independent commit outside the sync transaction and "never blocks
the pipeline — on any error we log and continue" (`tasks.py:1737–1869`, esp. `:1742`). So an
`ActionItem` can exist with no `ActionPlan`: not atomic, dual-read schema skew for clients, no
deprecation timeline. **Pre-launch, with nothing to migrate, we can simply finish the cutover:**
pick the DAG as canonical, stop dual-writing, retire the legacy path. Tracked here; sequenced as a
fast follow after isolation (§8).

## 5. Sub-problem 4c — isolation beyond Postgres (the other datastores)

**RLS secures Postgres only.** The tenant-isolation guarantee has to hold in every datastore, and
today it relies on the same logical-filter pattern elsewhere — so the backstop work is incomplete
if it stops at the database. Surface inventory:

| Store | Used for | Current scoping | Risk |
|---|---|---|---|
| **Qdrant** | KB / RAG vectors | **Single shared collection** (`vector_store.py:225`), payload filter at query time | **High** — same "forget the filter → cross-tenant RAG leakage" pattern as Postgres, no backstop |
| **S3** | Call audio | Tenant-prefixed key `recordings/{tenant_id}/…` (`s3_audio.py:65`); IAM scoping deferred | Medium — isolation by app convention, not enforced by the storage layer |
| **Elasticsearch** | Transcript full-text search | **Unverified** — index-per-tenant vs shared-index-with-filter | Unknown — must confirm |
| **Redis** | Broker, cache, sessions, rate-limit | Tenant-config cache namespaced (`tenant_cache.py`); session/rate-limit keys unverified | Low–Medium — mostly ephemeral, but verify session/key scoping |

Also note a **second scoping layer *below* tenant**: `kb_documents.customer_id` (nullable →
tenant-wide vs customer-only) and `user.agent_domains` / `manager_domains` (JSONB, not
DB-enforced). RLS on `tenant_id` does not cover within-tenant customer/domain scoping — that stays
application-enforced and needs its own test coverage.

**Highest-leverage move here:** funnel Qdrant access through a single search wrapper that *always*
injects the tenant payload filter (a choke point, like RLS is for Postgres), plus a test that a
cross-tenant vector search returns nothing. Consider per-tenant collections only if the shared
collection becomes a scale or blast-radius concern.

## 6. The pre-launch advantage (why now)

- **RLS now = free.** Zero rows to backfill, zero tenants to break, no live traffic. Post-launch,
  introducing RLS means auditing 89 tables against real data and risking a lockout incident.
- **Action-model cutover now = free.** No production `ActionItem` rows to migrate to the DAG.
- Both interact with the **fragile migration chain** (see the `ai_insights` incident) — doing them
  while the chain is short and data-free is far safer.

## 7. First implementation step (de-risk before the big roll-out)

A **one-table RLS spike**: enable RLS on `Interaction` end-to-end — set `app.current_tenant` in the
API request middleware *and* in the Celery task context, add the policy, and prove with a test that
a cross-tenant read returns zero rows through **both** the async and sync engines. This answers the
single make-or-break question (does the per-connection tenant GUC work cleanly with our pooling +
sync/async split?) cheaply. Pair it with a **scoping test guard** that inventories tenant-scoped
tables so we can measure the gap and prevent regressions immediately.

## 8. Chosen approach — Architecture Decision Record

**Status: Accepted** (isolation strategy), pending the §7 spike validating the GUC plumbing.
**Decision owner:** _tbd_ · **Date:** 2026-07.

### Decision
Keep the existing **pooled multi-tenancy** model (shared database, shared schema, `tenant_id`
column) and add **Postgres Row-Level Security as the runtime enforcement backstop now, pre-launch**,
extending the same fail-closed posture to the vector store (4c). We are **not** re-architecting to
schema-per-tenant or database-per-tenant at this time.

### Context
- Product is white-label B2B for **sales / CS / support teams** — SMB/mid-market, many tenants,
  cost-sensitive. Pooled is the mainstream, correct model for this profile.
- Data is **sensitive** (call recordings, transcripts, PII) — raises the stakes, doesn't change the
  shape; argues for RLS alongside the PII redaction, audit log, and token encryption already built.
- The schema is **already RLS-ready** (consistent `tenant_id`), so this is adding a missing
  enforcement *layer*, not fixing a broken foundation.
- Today: **213 manual filters, no backstop**, plus a shared Qdrant collection (4c). Pre-launch,
  with zero data, is the **cheapest this fix will ever be**.

### What we implement now
1. RLS on all tenant-scoped Postgres tables, tenant GUC set per API request and per Celery task.
2. Explicit allow-list for the legitimately-global tables (§3) + a documented **new-table
   checklist** so future tables declare scoped-or-global on creation.
3. A `BYPASSRLS` role (or scoped exemption) for Alembic migrations and legitimate cross-tenant
   admin/reseller surfaces.
4. A single tenant-filter choke point for Qdrant + cross-tenant vector-search test (4c).
5. A scoping **test guard** (inventory tenant-scoped tables/paths) + cross-tenant integration tests
   through both engines, as a permanent regression gate.
6. Verify/close the Elasticsearch and Redis scoping questions (4c).

### Explicitly rejected (for now)
- **Schema-per-tenant / database-per-tenant:** operational + migration cost (N schemas/DBs,
  N-times migrations) is not justified for the target market, and pooled+RLS gives a real backstop
  without it.
- **Full re-architecture:** the foundation is sound; this is a maturity gap (one missing layer),
  not a design error. An alarming filter count is not a reason to rebuild a coherent system.

### Consequences / trade-offs
- Adds DB-role and per-connection plumbing complexity; **couples to challenge #1's pool/loop
  lifecycle** (the GUC must ride every connection correctly) — hence the §7 spike first.
- RLS covers Postgres only; the guarantee is only as strong as its weakest store, so 4c
  (Qdrant/S3/ES/Redis) is part of "done," not a separate nicety.
- Within-tenant customer/domain scoping stays application-enforced (RLS is tenant-grain).

### Path forward (future — do NOT build now, just keep the door open)
Stay **pooled + RLS** as the default. Re-evaluate toward a **hybrid** model (pool the many, silo
the few) when — and only when — a concrete trigger fires:
- **T1 — Compliance/enterprise:** a regulated or enterprise buyer contractually requires physical,
  single-tenant data separation (HIPAA/PCI/FedRAMP, "your data in its own database").
- **T2 — Scale outlier:** a single tenant's data volume needs its own performance/backup envelope.
- **T3 — Reseller partition:** a white-label reseller requires their sub-tenants physically
  separated.

The evolution is additive: route the triggering tenant to its own database behind the *same*
application code, keeping pooled+RLS for everyone else. **Pooled+RLS forecloses none of this** —
which is exactly why it's safe to commit to now. Revisit this ADR if any trigger appears.

## 9. Implementation increments

_To be sequenced from §7 → §8 once the spike lands. Each increment: red tests first → implement →
verify. Rough order: (1) one-table RLS spike + GUC plumbing, (2) scoping test guard, (3) roll RLS to
all tenant-scoped tables + global allow-list, (4) Qdrant choke point + test, (5) ES/Redis
verification, (6) action-model cutover (4b)._
