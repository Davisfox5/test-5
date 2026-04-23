# LINDA operator runbook

On-call reference for the common production procedures. Follow in order; every
step is designed to be safe to retry.

---

## 1. Deploy a new release

The happy path is automatic on merge-to-main. Manual promote steps are below.

### Staging (automatic)

1. Merge PR to `main` on GitHub.
2. Watch `.github/workflows/ci-cd.yml` — the `test → build → deploy_staging`
   chain runs. `deploy_staging` posts the new image tag to
   `$STAGING_DEPLOY_HOOK` and polls `/api/v1/ready/deep` for up to 5 minutes.
3. If the workflow fails on `deploy_staging`, the previous image is still
   running — no rollback needed. Fix forward.

### Production (manual)

1. In GitHub → Actions → **CI/CD** → **Run workflow**.
2. Branch: `main`. Input `deploy_env`: `production`.
3. The production environment is configured to require an approver. Approve in
   the GitHub UI. Watch the `deploy_production` job poll
   `/api/v1/ready/deep` for up to 10 minutes.
4. Post-deploy: run the smoke harness against production (see §7).

---

## 2. Roll back

Production rollback is "promote the previous image tag." We never roll back by
force-pushing main.

1. Find the previous SHA from the container registry (or `git log --oneline
   origin/main`). Short SHA is 12 chars — that's the image tag.
2. GitHub → Actions → **CI/CD** → **Run workflow**.
3. Override the `deploy_env` input with `production`; the job will rebuild
   the image from the older commit and redeploy.
4. If the previous image is still tagged in the registry, a faster path is to
   invoke the deploy hook directly:

   ```bash
   curl -X POST "$PRODUCTION_DEPLOY_HOOK" \
        -H 'Content-Type: application/json' \
        -d '{"release":"<previous-short-sha>"}'
   ```

5. Verify with `curl -s $PRODUCTION_URL/api/v1/ready/deep | jq`.

**Database migrations**: rollback only reverts application code. If the
release included a migration, follow §4 for the database step; don't assume
the old code is compatible with the new schema without checking.

---

## 3. Drain a stuck Celery queue

Symptom: `linda_celery_queue_depth{queue="celery"}` growing unbounded, workers
OOM-restarting, or tasks showing `status=retry` for long stretches.

1. Inspect queue depth: `redis-cli LLEN celery` (or whatever the queue name
   is; `sample_queue_depth` logs them). If depth is under 1k, you probably
   don't need to drain — give it 5 minutes.
2. Inspect active tasks: `celery -A backend.app.tasks inspect active`.
3. If a single task type is stuck in a loop:
   1. Identify the task: Sentry will have the traceback. Cross-check with
      `linda_pipeline_stage_seconds` histogram — a P99 spike points at the
      bad stage.
   2. Push a hotfix that raises immediately for the stuck argument shape, or
      add a `dead_letter_after_n_retries` guard.
   3. Redeploy.
4. If the entire queue is poisoned (rare — usually a malformed Redis message):
   ```bash
   # ONLY after confirming we don't mind losing in-flight work. The
   # backup task writes retryable state to Postgres so this is safe
   # for pipeline tasks; webhook deliveries might need replay.
   redis-cli DEL celery
   ```
5. Scale workers if the depth is legit backpressure: bump the worker replica
   count in your orchestrator and watch the depth drain.

---

## 4. Apply a database migration

All migrations are Alembic-managed.

1. The migration file lives in `backend/alembic/versions/`. Inspect its
   `upgrade()`/`downgrade()` before shipping so you know the rollback plan.
2. Migrations run **automatically** on container start in our default
   images (see the entrypoint). If you want to run them manually:
   ```bash
   alembic -c backend/alembic.ini upgrade head
   ```
3. For a long migration (index rebuild on a big table), run it out-of-band
   with `SET statement_timeout = 0;` rather than letting the deploy hang.
4. Downgrade if needed:
   ```bash
   alembic -c backend/alembic.ini downgrade -1
   ```

   Downgrades are tested in CI but review the specific migration's
   `downgrade()` first — some drop-column operations can't bring the data
   back.

---

## 5. Rotate the Fernet token-encryption key

All CRM + email tokens are Fernet-encrypted at rest via
`backend/app/services/token_crypto.py`. Rotation is a two-step dance.

1. Generate the new key:
   ```bash
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```
2. Update the env var on all backend + worker replicas:
   ```
   TOKEN_ENCRYPTION_KEY=<new>
   TOKEN_ENCRYPTION_KEYS_FALLBACK=<old>,<older>  # accept old keys during rotation
   ```
   `token_crypto` reads both: it encrypts new tokens with the primary, and
   decrypts with any key in the fallback list.
3. Redeploy all replicas.
4. Run the re-encrypt task to rewrite every row with the new key:
   ```bash
   celery -A backend.app.tasks call reencrypt_all_tokens
   ```
5. After 24 hours (well after every cached token has been re-encrypted),
   drop the old key from `TOKEN_ENCRYPTION_KEYS_FALLBACK`.

---

## 6. Add a new tenant

1. Use the admin signup flow if the tenant's executive is creating the
   account themselves (`POST /api/v1/signup`).
2. For internally-provisioned tenants:
   ```sql
   INSERT INTO tenants (id, name, slug, plan_tier, features_enabled)
   VALUES (gen_random_uuid(), 'Tenant Name', 'tenant-name', 'growth', '{}'::jsonb);
   ```
   Then create an executive user + API key via the admin endpoints.
3. If the tenant needs a specific feature flag flipped on from day one
   (e.g. `paralinguistic_live` for a high-touch pilot), do it via the admin
   UI (Settings → Feature Flags) so the change is auditable.
4. For CRM-connected tenants, they still need to complete the OAuth handshake
   themselves — we don't have a programmatic shortcut for that.

---

## 7. Run the smoke harness

The smoke harness lives in `scripts/smoke.py` and is the fastest way to verify
a deploy didn't break anything customer-visible.

```bash
python scripts/smoke.py \
    --base-url $PRODUCTION_URL \
    --api-key $LINDA_PROD_SMOKE_KEY \
    --audio-url https://public.example.com/linda-smoke-3s.wav \
    --crm pipedrive,hubspot
```

Exit code 0 = all checks pass; 1 = at least one failed. Treat a failure as
blocking for the deploy.

---

## 8. Paralinguistic rollout checklist

Before enabling `paralinguistic_live` for a new tenant:

1. Run the corpus harness against the current build:
   ```bash
   python -m backend.app.services.paralinguistics_corpus \
       corpora/2026-04-baseline.yaml \
       --min-f1 0.70
   ```
2. If F1 is below 0.70, **don't enable** — investigate which alert kind
   regressed (per-kind stats are in the report) and fix before rollout.
3. Load-test at expected peak concurrency:
   ```bash
   python scripts/loadtest_live_paralinguistic.py \
       --clip corpora/clips/example.wav \
       --concurrency 25 \
       --duration-sec 60
   ```
   Track `cpu_mean_pct` and `snapshot_ms_p99`. p99 > 500 ms is a red flag —
   scanners will miss events; scale workers before enabling.

---

## 9. GDPR request handling

- **Article 15 / 20 (export)**: hit
  `GET /api/v1/tenants/{tenant_id}/export?reason=<short-note>` from an admin
  session. Response streams NDJSON; pipe to a file.
- **Article 17 (delete)**: `DELETE /api/v1/tenants/{tenant_id}` with body
  `{"reason":"…","confirm_tenant_name":"<exact tenant name>"}`. The confirm
  string stops accidental wrong-tenant deletes.
- Every call writes a `tenant_dataops_log` row — review before closing the
  ticket so the audit trail is complete.

---

## 10. Sentry and metrics

- Sentry: filter by `tenant_id` or `request_id` tag. The request id echoes
  on every API response as `X-Request-Id`, so tenant reports "I saw error
  abc-123" map directly.
- Metrics: `/metrics` endpoint exposes Prometheus scrape targets. Dashboards
  should at minimum watch:
  - `linda_pipeline_runs_total{status="failure"}` rate
  - `linda_celery_queue_depth` per queue
  - `linda_transcription_failures_total` per engine
  - `linda_live_sessions_active` per provider
  - `linda_crm_writeback_outcomes_total{status="error"}` rate
