# Deployment configuration guide

Two places secrets live:

1. **Runtime env vars** — read by the API + Celery worker containers at boot.
   Hosted wherever you deploy (Fly secrets, AWS Secrets Manager, ECS
   parameter store, Kubernetes secret, Render env group…).
2. **GitHub Actions secrets + variables** — referenced only by
   `.github/workflows/ci-cd.yml`. Repo Settings → Secrets and variables →
   Actions.

If a value is used both by CI *and* by runtime, set it in both places.

---

## What you need to procure

Grouped by provider. Everything marked **required** must be in place before
first customer traffic; optional items light up specific features.

### Required — core infrastructure

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| **Postgres database** | Neon / RDS / Supabase / self-hosted | `DATABASE_URL` |
| **Redis** | Upstash / ElastiCache / self-hosted | `REDIS_URL` |
| **Session JWT secret** | generate: `python -c 'import secrets; print(secrets.token_urlsafe(48))'` | `SESSION_JWT_SECRET` |
| **Fernet token-encryption key** | generate: `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` | `TOKEN_ENCRYPTION_KEY` |
| **S3 staging bucket** | AWS console — create bucket, private ACL, lifecycle rule: delete objects > 24 h | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_S3_BUCKET` |

### Required — AI + speech

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| **Anthropic API key** | console.anthropic.com → API Keys | `ANTHROPIC_API_KEY` |
| **Deepgram API key** | console.deepgram.com → API Keys | `DEEPGRAM_API_KEY` |
| **Hugging Face token** | hf.co/settings/tokens → create *read* token, then accept licence at hf.co/pyannote/speaker-diarization-3.1 | `HUGGINGFACE_TOKEN` |
| **Voyage embeddings key** | voyageai.com → API keys | `VOYAGE_API_KEY` |

### Required — auth

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| **Clerk keys** | clerk.com → API keys. Both publishable + secret. | `CLERK_SECRET_KEY`, `CLERK_PUBLISHABLE_KEY` |

### Required — observability + error monitoring

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| **Sentry DSN** | sentry.io → Settings → Projects → [project] → Client Keys (DSN) | `SENTRY_DSN`, plus `ENVIRONMENT=production`, `RELEASE_VERSION=<sha>` set in your orchestrator |

### Optional — billing (required only for paid plans)

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| Stripe API key | dashboard.stripe.com → Developers → API keys | `STRIPE_API_KEY` |
| Stripe webhook secret | Developers → Webhooks → Add endpoint → reveal signing secret | `STRIPE_WEBHOOK_SECRET` |
| Stripe price IDs | Products → create products for each tier, copy `price_…` IDs | `STRIPE_PRICE_SANDBOX` / `_STARTER` / `_GROWTH` / `_ENTERPRISE` |

### Optional — telephony (any subset; enable the providers you use)

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| Twilio | console.twilio.com → Account → API keys & tokens | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` |
| SignalWire | signalwire.com → API credentials | `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_TOKEN` |
| Telnyx | portal.telnyx.com → API keys | `TELNYX_API_KEY` |

For each provider, also configure the inbound webhook URL in their dashboard
to point at `https://<your-host>/api/v1/telephony/<provider>/voice`.

### Optional — KB connectors

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| Google Drive OAuth | console.cloud.google.com → APIs & Services → Credentials → OAuth client | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |
| Microsoft Graph OAuth | portal.azure.com → Entra ID → App registrations | `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` |

Confluence + MCP connectors are configured per-tenant in the admin UI, not
via env vars.

### Optional — CRM

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| HubSpot | developers.hubspot.com → Apps → Create app | `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET` |
| Salesforce | Setup → App Manager → New Connected App | `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET` |
| Pipedrive | developers.pipedrive.com → Apps | `PIPEDRIVE_CLIENT_ID`, `PIPEDRIVE_CLIENT_SECRET` |

For each, set the OAuth redirect URI in the provider's dashboard to
`https://<your-host>/api/v1/integrations/<provider>/callback`.

### Optional — enrichment

| What | Where to get it | Env var(s) |
|------|-----------------|------------|
| People Data Labs | peopledatalabs.com → API | `PDL_API_KEY` |
| Apollo | apollo.io → API | `APOLLO_API_KEY` |

---

## GitHub Actions configuration

Repo → **Settings** → **Secrets and variables** → **Actions**.

### Variables (non-sensitive)

| Name | Example | Used by |
|------|---------|---------|
| `CONTAINER_REGISTRY` | `ghcr.io/davisfox5` | CI build job — where image is pushed |
| `STAGING_URL` | `https://staging.linda.example.com` | post-deploy readiness probe |
| `PRODUCTION_URL` | `https://api.linda.example.com` | post-deploy readiness probe |

For GHCR pushes the workflow uses the auto-issued `GITHUB_TOKEN` with
`packages: write`, so no registry credentials are needed in repo secrets.
For ECR or another registry, add a `CONTAINER_REGISTRY_USER` /
`CONTAINER_REGISTRY_PASSWORD` pair and swap them into the `docker/login-action`
step in `.github/workflows/ci-cd.yml`.

### Secrets

| Name | Value | Used by |
|------|-------|---------|
| `STAGING_DEPLOY_HOOK` | webhook URL your hosting platform provides | CI deploy_staging |
| `PRODUCTION_DEPLOY_HOOK` | webhook URL for production | CI deploy_production |

### GitHub environments (optional but recommended)

Settings → Environments → **production** → add required reviewers. Once
configured, the `deploy_production` job waits for human approval.

---

## Runtime env configuration

Wherever you host (Fly, Render, Railway, ECS, Kubernetes…), set:

- Everything in `.env.example` that applies to the providers you're using.
- `ENVIRONMENT=production`
- `RELEASE_VERSION=<short-git-sha>` (your deploy hook should populate this
  from the image tag CI built).
- `LOG_FORMAT=json` (so your log aggregator can parse it).
- `LOG_LEVEL=INFO`.
- `SENTRY_TRACES_SAMPLE_RATE=0.1` for production (lower if event volume
  is expensive; higher for smaller tenants where you want full traces).

Worker processes need the **same** env as the API process — they share a
database, Redis, and the model caches. The only worker-specific var is:

- `LINDA_WORKER_WARMUP=1` (default) — preload pyannote + SpeechBrain at
  worker start so the first task doesn't pay a cold-start tax. Set to `0`
  on beat-only workers that never run audio tasks.

---

## Minimum first-deploy checklist

If you just want to get a staging environment up, this is the short list:

**GitHub** (5 min)
- [ ] `vars.CONTAINER_REGISTRY` (e.g. `ghcr.io/davisfox5`)
- [ ] `vars.STAGING_URL`
- [ ] `secrets.STAGING_DEPLOY_HOOK`

**Runtime** (15 min)
- [ ] `DATABASE_URL`, `REDIS_URL`
- [ ] `SESSION_JWT_SECRET`, `TOKEN_ENCRYPTION_KEY` (both generated locally)
- [ ] `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `HUGGINGFACE_TOKEN`, `VOYAGE_API_KEY`
- [ ] `CLERK_SECRET_KEY`, `CLERK_PUBLISHABLE_KEY`
- [ ] `SENTRY_DSN`, `ENVIRONMENT=staging`
- [ ] `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_S3_BUCKET`
- [ ] `PUBLIC_WEBHOOK_BASE_URL=<staging URL>`

Everything else (CRM, telephony, KB connectors) can land tenant-by-tenant as
customers turn them on. Nothing *blocks* a staging deploy except those ~15
values.

---

## Generating secrets locally

For the two values that are yours to generate (not procured), use these
one-liners:

```bash
# Session JWT secret
python -c 'import secrets; print(secrets.token_urlsafe(48))'

# Fernet token-encryption key
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Both are 32-byte random values encoded as strings. Keep them in your
secret store; don't commit them anywhere — even `.env` files should be in
`.gitignore`.

---

## Rotating secrets

Each runtime secret has a rotation note:

- `TOKEN_ENCRYPTION_KEY` — two-key rotation via `TOKEN_ENCRYPTION_KEYS_FALLBACK`.
  See `docs/runbook.md` §5.
- `SESSION_JWT_SECRET` — rotate by adding the new one, letting the TTL
  elapse (default 12 h), then removing the old one. Sessions reissued
  mid-window stay valid.
- `CLERK_SECRET_KEY`, `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `VOYAGE_API_KEY` —
  provider dashboards let you generate a new key without revoking the old
  one; flip the env var, redeploy, then revoke the old key.
- Provider OAuth client secrets (`GOOGLE_CLIENT_SECRET`, etc.) — same
  pattern: generate new, deploy, revoke old. Expect a brief window where
  existing tenants' refresh tokens need to be re-minted.
