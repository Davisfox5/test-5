# Security, Efficiency & Hygiene Audit — 2026-06-11

Deep-dive review of the full repository covering security vulnerabilities,
correctness bugs, AI/LLM cost-efficiency, and repository organization.
Every finding below was verified against the code (file:line); findings
from automated review that did not hold up under verification were dropped.

Two sections: **Fixed in this branch** (shipped with this audit) and
**Proposed** (needs an owner decision, runtime measurement, or carries
deploy risk that shouldn't ride along with an audit commit).

---

## 1. Fixed in this branch

### 1.1 CRITICAL — CRM sync results were never committed (data loss)

`backend/app/tasks.py` — `crm_sync_tenant` and `crm_sync_daily` opened an
`async_session()`, ran `sync_crm_for_tenant(...)` (which only ever calls
`flush()`, never `commit()` — by design, the API route's `get_db`
dependency commits for it), and returned. Closing the session without a
commit rolls everything back, so **every Celery-driven CRM sync (including
the nightly fan-out) silently persisted nothing** — no upserted
customers/contacts, not even the `CrmSyncLog` row that would have made the
failure visible. The task return value reported healthy-looking counts.

Fix: explicit `await db.commit()` in `crm_sync_tenant`; per-tenant
`commit()` + `rollback()`-on-error in the `crm_sync_daily` loop (which also
fixes a latent second bug: one tenant's failure left the shared session in
an aborted-transaction state, poisoning every subsequent tenant in the
loop). The neighbouring `crm_writeback` task already committed — the
pattern was known, just missed here.

### 1.2 HIGH — Scoped API keys could call admin write endpoints

API-key principals resolve to a synthetic `role="admin"`
(`backend/app/auth.py:512`), and the `/admin/*` router was gated only by
`require_role("admin")` (`backend/app/main.py`). Scope checks
(`require_scope`) were never applied to the admin router — so an API key
deliberately created with a read-only scope list (e.g. `["analytics:read"]`)
could still hit all 17 mutating admin endpoints (tenant context rewrite,
tenant settings/tier changes, demo seeding, diagnostics). That defeats the
documented fail-closed contract in `auth.py` ("every POST/PATCH/PUT/DELETE
will 403" for blank scopes).

Fix: new `require_scope_for_writes("settings:write")` dependency
(method-aware variant of `require_scope`; GET/HEAD/OPTIONS pass, mutating
methods require the scope for API-key callers, human sessions bypass as
before) applied at the admin router include. Documented in
`docs/api_key_scope_map.yaml`. The webhooks/CRM routers already had
per-route scope gates and needed no change.

### 1.3 MEDIUM — Non-constant-time webhook secret comparisons

`backend/app/api/email_push.py` compared the Gmail Pub/Sub push token
(`token != expected`, line 114) and the Microsoft Graph `clientState`
(line 191) with ordinary string equality. These are the only
authentication on two public unauthenticated endpoints; non-constant-time
comparison leaks timing information that allows byte-by-byte brute force
of the shared secret, after which an attacker can inject fake mailbox
notifications (email-ingest poisoning). Every other webhook in the
codebase (Stripe, Twilio, Telnyx, AudioHook) already used
`hmac.compare_digest`. Fix: both comparisons now use `hmac.compare_digest`.

### 1.4 MEDIUM — CORS wildcard headers/methods with credentials

`backend/app/main.py` set `allow_methods=["*"]` and `allow_headers=["*"]`
with `allow_credentials=True`. The wildcard reflects whatever the
preflight asks for, silently whitelisting any future custom header — a
footgun if any later code trusts a custom header. Fix: explicit lists
(`Authorization`, `Content-Type`, `X-Request-Id`, `X-Tenant-Id` — the
SPA only sends the first two; the latter two feed
`RequestContextMiddleware`), plus `max_age=3600` so browsers cache
preflights (fewer OPTIONS round-trips → marginally faster SPA).

### 1.5 MEDIUM — Naive `datetime.utcnow()` written into timestamptz columns

Every datetime column is `DateTime(timezone=True)` and most of the
codebase correctly uses `datetime.now(timezone.utc)`, but ~30 call sites
across 15 files (`api/action_plans.py`, `api/experiments.py`,
`api/interactions.py`, `services/variant_rollout.py`,
`services/regression_watchdog.py`, …) still used the deprecated, naive
`datetime.utcnow()`. Naive values assigned to timestamptz columns or
compared against aware values are driver-dependent at best
(`TypeError: can't compare offset-naive and offset-aware datetimes` at
worst — e.g. `regression_watchdog.py` compared its naive `now` against
aware DB rows). Fixed mechanically across `backend/app/`; seed/demo
scripts left untouched (dev-only, sync driver).

### 1.6 LOW — Cache-busting JSON serialization in email classifier prompt

`backend/app/services/email_classifier.py:187` serialized email headers
with `json.dumps(...)` without `sort_keys=True` inside the prompt. Header
ordering varies between messages, producing byte-different prompts for
semantically identical content and defeating Anthropic prompt-cache prefix
matching. Fixed with `sort_keys=True`.

### 1.7 Repo hygiene

- `ARCHITECTURE.md` (self-declared **OUTDATED** in its own header, wrong
  stack/endpoints) → moved to `docs/archive/ARCHITECTURE.md`.
- One-off root-level scripts `backend/seed_prompt_variants.py`,
  `backend/analyze_seed.py`, `backend/backfill_ai_trends.py` → moved to
  `backend/scripts/` alongside the other operational scripts (imports
  updated in `tests/test_backfill_ai_trends.py`, README updated).
  `backend/seed.py` stays put — it's the documented seed runner.
- `clerk-backend-api` removed from `requirements.txt`: it is imported
  nowhere; Clerk JWTs are verified directly against JWKS via
  `python-jose` (`auth.py`). Smaller image, one less dependency to patch.

**Validation:** full test suite run after all changes:
**1269 passed, 7 skipped, 0 failed** (two test modules excluded for
sandbox-only missing native deps — deepgram SDK and Playwright — exactly
as CI excludes the latter).

---

## 2. Proposed changes (not applied — need owner decision/measurement)

### 2.1 Security

| Sev | Where | Finding | Suggested fix |
|---|---|---|---|
| **HIGH** | `backend/app/db.py:24-27` | TLS certificate verification for the Postgres connection is disabled (`check_hostname=False`, `CERT_NONE`) whenever `sslmode=` appears in the URL — encrypted but unauthenticated, i.e. MITM-able. | Use `ssl.create_default_context()` unmodified (Neon presents a publicly-trusted cert). Needs a staging deploy test before flipping — if the container lacks CA certs it will break connectivity, which is why it isn't changed in this branch. |
| LOW | `backend/app/api/email_push.py` | Push endpoints rely on a single static shared secret + IP rate-limit. | Defense in depth: verify Pub/Sub OIDC JWT (Google supports signed push) instead of the `?token=` query secret, which also keeps the secret out of access logs. |
| INFO | `apps/app/fly.toml` | Clerk publishable key hardcoded (currently `pk_test_*`, non-secret by design). | Keep, but add a quarterly check that it never becomes a prod key. |

Reviewed and found **solid** (no action): JWT alg pinning (HS256 list),
bcrypt(12) password hashing, Fernet token encryption at rest, tenant
scoping on data queries, Stripe/Twilio/Telnyx/AudioHook signature
verification (constant-time, fail-closed, rotation support), SSRF guards
on outbound webhooks (loopback/RFC1918 rejection), single-use TTL'd
WebSocket tickets, SCIM scope gating, S3 private ACL + signed URLs,
parameterized SQL throughout, GDPR delete confirmation gate + pre-delete
audit log.

### 2.2 LLM cost-efficiency

The fundamentals here are genuinely good: ~26 of 28 `messages.create`
sites use `cache_control: ephemeral`, model tiering
(Haiku→Sonnet→Opus) is enforced by `model_router.py`, per-call telemetry
records cache hits/truncations, and learned per-call-site `max_tokens`
ceilings already trim output spend. The remaining opportunities:

1. **Message Batches API for offline jobs (biggest lever, ~50% discount).**
   `model_router.submit_batch()` (line 232) is fully built but has **zero
   callers**. The LLM-judge evaluation (`llm_judge.py`, one synchronous
   call per analyzed interaction via the `evaluate_analysis` Celery task)
   and the per-tenant KB brief refiner are non-latency-sensitive and batch
   cleanly: accumulate pending evaluations, submit one batch, poll, write
   results. At 1K interactions/day this halves the judge's token bill.
2. **Cache-aware retry in `ai_analysis.py` (~line 1183/1215).** The
   truncation retry re-sends the identical large system+KB context; on a
   cache miss both attempts pay full price. Check
   `usage.cache_read_input_tokens` on the retry and prefer escalating the
   output budget over a third resend.
3. **KB orchestrator oversized-doc gate (`kb/orchestrator.py`).** Docs up
   to 50K chars (~12.5K input tokens) are sent to the parse model; parse
   failures fall back to plain chunking, wasting the whole input spend.
   Add telemetry on parse success by doc size, and route oversized docs
   straight to plain chunking.
4. **Content-hash skip for re-embedding (`kb/` ingest).** Re-synced KB
   docs are re-embedded even when unchanged. Store a SHA-256 of the doc
   content on `KBDocument` and skip embedding when it matches.
5. **Model IDs via settings.** `claude-haiku-4-5-20251001` /
   `claude-sonnet-4-6` / `claude-opus-4-7` are hardcoded in ~20 files
   (with three "keep in sync" mirror dicts). Centralize in
   `config.Settings` so model upgrades are a config change, not a 20-file
   sweep. (All current IDs are valid/non-deprecated; Opus tier is defined
   but unused in live paths — good cost discipline.)

### 2.3 Correctness / operations

| Sev | Where | Finding |
|---|---|---|
| MED | `backend/app/tasks.py` (several `@celery_app.task` without `max_retries`) | Scheduled tasks (e.g. `email_ingest_poll`) rely on Celery defaults; add explicit `max_retries` for consistency with the tasks that set it. |
| MED | `backend/app/tasks.py` worker warmup | Non-fatal startup failures (e.g. emotion-classifier load) logged at DEBUG — invisible in prod while silently degrading every extraction. Log at WARNING and/or add a startup-warning metric. |
| LOW | `backend/app/api/stripe_webhook.py:96` | Outbound Stripe API errors surface as 502 to our client; arguably correct as a gateway error, but a 503 + structured detail would prevent client retry loops on permanent errors. |
| LOW | `backend/app/services/crm/writeback.py` | Outcome metrics can double-count kinds on partial failure; label per-operation. |

(Claims from automated review that did **not** survive verification and
are deliberately excluded: alleged unbounded `.all()` on million-row
tables — the cited queries are bounded per-tenant/contact lookups; an
alleged N+1 contact loop — it's one lookup per pipeline run; alleged
event-loop blocking `requests.post` — it's in a sync Celery code path.)

### 2.4 CI/CD & repo

1. **No lint or type-check in CI** (`.github/workflows/ci-cd.yml` runs
   pytest only). Add `ruff check backend/` and the SPA's existing
   `tsc --noEmit` script as CI jobs. Start ruff with a minimal rule set so
   the first run doesn't fail on legacy style.
2. **No dependency vulnerability scanning.** Add `pip-audit` (or GitHub
   Dependabot) for `requirements.txt` and `npm audit` for `apps/app`.
3. `scripts/smoke.py`, `scripts/live_action_plan_smoke.py`,
   `scripts/loadtest_live_paralinguistic.py` are unreferenced — likely
   still useful operationally; keep, or document in `scripts/README`.
   `docs/sessions/` is historical handoff material; fine where it is or
   move under `docs/archive/`.
4. `playwright` (needed by `tests/sandbox_demo_ui_test.py`, excluded from
   CI) isn't declared anywhere — add a comment or an optional dev
   requirements file.

Already in good shape (no action): Dockerfile layer ordering + multi-stage
build + non-root runtime, `.gitignore`/`.dockerignore` coverage, no
committed secrets anywhere (repo-wide scan), pinned-range requirements
with per-integration ownership blocks, production deploys gated behind
manual `workflow_dispatch`, cheap liveness vs. deep readiness probe
split, fly.toml cost tuning (auto-stop, worker concurrency, SIPREC VM
disabled while unused).
