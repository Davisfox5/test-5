# LINDA — Architecture

LINDA is a white-label B2B SaaS platform that ingests customer-conversation audio and
text (phone, VoIP, video-conferencing recordings, uploaded transcripts, email), transcribes
it, and runs AI analysis to produce next steps, follow-ups, sentiment/churn/upsell signals,
QA scorecards, and live coaching. Sold to sales, customer-success, and support teams.

---

> ## 📌 Maintenance instruction (read me first)
>
> **This document must stay in sync with the code. If you change the architecture, update this file in the same PR.**
>
> Treat the codebase — not this doc — as the ultimate source of truth, and reconcile any drift you notice:
> - **Routers / endpoints:** `backend/app/main.py` (`app.include_router(...)` calls) and `backend/app/api/`
> - **Data model:** `backend/app/models.py` (single module; every `__tablename__` is a table)
> - **Scheduled work:** the `beat_schedule` dict in `backend/app/tasks.py`
> - **Process model & deploy:** `fly.toml` (`[processes]`), `Dockerfile`
> - **Tech stack / external services:** `requirements.txt`, `apps/app/package.json`
>
> When you touch any of those, re-read the relevant section below and fix it if it no longer matches.
> A wrong architecture doc is worse than none — it sends future readers (human and AI) down dead ends.
> If you cannot verify a claim here against the code, delete or flag it rather than leave it asserted.

---

## 1. System shape

```
                    ┌─────────────────────────────────────────────┐
   Browser ───────► │  apps/app  — Next.js 15 / React 19 SPA       │
   (product users)  │  (Clerk auth, TanStack Query, Tailwind)      │
                    └───────────────┬─────────────────────────────┘
                                    │  HTTPS  /api/v1/*  +  WebSocket
                    ┌───────────────▼─────────────────────────────┐
   Prospects ─────► │  website/  — static marketing + demo site    │
   (marketing)      │  (vanilla JS, nginx; talks to the same API)  │
                    └───────────────┬─────────────────────────────┘
                                    │
                    ┌───────────────▼─────────────────────────────┐
                    │  FastAPI app  (backend.app.main:app)         │
                    │  ~56 routers under /api/v1 + WebSocket        │
                    └───┬───────────────────────────┬──────────────┘
                        │ enqueue (Redis broker)     │ read/write
                  ┌─────▼──────┐              ┌───────▼───────────────────────────┐
                  │  Celery    │              │  Postgres · Redis · Qdrant ·       │
                  │  worker +  │─────────────►│  Elasticsearch · S3 (optional)     │
                  │  beat      │              └────────────────────────────────────┘
                  └─────┬──────┘
                        │ external APIs
        Anthropic (Claude) · Deepgram (ASR) · Stripe · Clerk · telephony providers
```

## 2. Backend (`backend/app/`)

A single FastAPI application plus a Celery worker/beat, sharing the same code and models.

| Path | Role |
|---|---|
| `main.py` | App factory + `lifespan`; registers ~56 routers under `settings.API_V1_PREFIX` (`/api/v1`); `RequestContextMiddleware` + CORS; `GET /metrics` (Prometheus), health, and the WebSocket router |
| `models.py` | **All** SQLAlchemy models in one module — 89 tables. The central entity is **`Interaction`** (a call / email / uploaded transcript). |
| `tasks.py` | Celery app (`celery_app`), every background task, and the `beat_schedule` |
| `api/` | One module per router group (`interactions.py`, `manager.py`, `knowledge_base.py`, `scorecards.py`, `action_plans.py`, `telephony.py`, `sso_provisioning.py`, `scim.py`, `stripe_webhook.py`, `siprec.py`, `uc_telephony.py`, `teams_recording.py`, `audiohook.py`, `websocket.py`, …) |
| `services/` | ~100 service modules + subpackages (`action_plan/`, `audio/`, `crm/`, `email/`, `email_ingest/`, `kb/`, `meeting_scheduler/`, `telephony/`, `teams_recording/`). Business logic lives here; routers stay thin. |
| `auth.py` | API-key + per-user auth, tenant scoping, scope checks |
| `config.py` | Pydantic-settings `Settings`; all env-driven config |
| `db.py` | SQLAlchemy engine/session setup (async for the API, sync engine for Celery tasks) |
| `observability.py`, `logging_setup.py` | Sentry (optional) + structured logging |
| `plans.py` | Subscription tiers (canonical: **Sandbox / Starter / Growth / Enterprise**) and Stripe mapping |

### 2.1 Data model highlights

`Interaction` is the hub. Around it: `Tenant`, `User`, `Customer`, `Contact`, `SupportCase`,
`InteractionScore` / `InteractionSnippet` / `InteractionComment`, knowledge base
(`KBDocument` / `KBChunk`), CRM (`CrmDealRecord` / `CrmSyncLog`), and the action model.

> **Action model is mid-migration.** `ActionItem` (`action_items`) is the **legacy** flat-list
> shape; the current shape is the DAG of `ActionPlan` → `ActionStep` (`action_plans` /
> `action_steps`, plus `StepArtifact` / `StepResponse`). Both coexist during cutover — see
> `models.py` for the authoritative status before assuming either is dead.

### 2.2 Async pipeline & scheduled work

Ingest → transcription (Deepgram, or self-hosted Whisper) → AI analysis (Claude) →
entity resolution → signal/scorecard/snippet generation → notifications/webhooks. The heavy
steps run as Celery tasks off a Redis broker, across three queues: **`priority`, `default`,
`batch`**.

`beat_schedule` (in `tasks.py`) drives ~30 recurring jobs — e.g. weekly tenant insights,
daily/weekly orchestration, outcomes backfill, calibration / IRT / churn-model fits, audio
& event retention sweeps, email-ingest polling, feedback-stream consumption, CRM sync,
WER computation, and A/B variant winner selection. **The dict is the source of truth for
what runs and when.**

## 3. Frontend

- **`apps/app/`** — the product SPA. **Next.js 15 + React 19**, Clerk for auth
  (`@clerk/nextjs`), TanStack Query for server state, Tailwind. Route groups under
  `src/app/(app)` (authed product) and `src/app/(auth)`; feature components under
  `src/components/`.
- **`website/`** — the static marketing site **and** an interactive demo, built with vanilla
  JS modules (`website/js/*`) served by nginx. It calls the same `/api/v1` backend and stores
  its API token in the `linda-api-key` localStorage key (a one-time migration in
  `demo.js` / `main.js` bridges any old `callsight-*` keys for returning visitors).

## 4. Data stores & external services

| System | Used for |
|---|---|
| **Postgres** | Primary datastore (SQLAlchemy 2.0, migrations via Alembic in `backend/alembic/`) |
| **Redis** | Celery broker, pub/sub, sessions, rate limiting |
| **Qdrant** | Knowledge-base RAG vectors |
| **Elasticsearch** | Full-text transcript search |
| **S3** (optional) | Audio storage; falls back to a local dir (`$AUDIO_LOCAL_DIR`) when boto3/creds absent |
| **Anthropic** | Claude (Haiku / Sonnet / Opus) for analysis, triage, coaching, judging |
| **Deepgram** | Batch + streaming ASR and diarization |
| **Presidio + spaCy** | PII detection / redaction |
| **Stripe** | Billing / subscriptions / webhooks |
| **Clerk** | Frontend authentication |
| **Telephony** | SIPREC, UC providers (Zoom/Teams/Webex/RingCentral), Twilio/Telnyx, Genesys AudioHook |

## 5. Process model & deployment

Deployed on **Fly.io**. `fly.toml` `[processes]` defines three roles off one image:

- **`api`** — `uvicorn backend.app.main:app` (2 workers, proxy-headers)
- **`worker`** — `celery … worker -Q priority,default,batch`
- **`beat`** — `celery … beat` (schedule persisted to the `/data` volume)

`Dockerfile` builds the backend image (default `CMD` runs uvicorn). The Next.js SPA
(`apps/app`) and the static `website/` deploy as their own Fly apps. CI deploys the backend
and the SPA together — keep both wired in the deploy workflow.

## 6. Observability

Custom app metrics (named `linda_*`) are defined in `backend/app/services/metrics.py`
and exposed at `GET /metrics` in Prometheus format:

- The **api** process serves `/metrics` via FastAPI on port 8000.
- The **worker** and **beat** processes don't run FastAPI, so they start a small
  prometheus_client HTTP server (also port 8000, a free port on their machines) from
  the `worker_init` / `beat_init` hooks in `tasks.py`.
- Because the api runs 2 uvicorn workers and celery runs prefork children, metrics use
  **prometheus_client multiprocess mode**: `PROMETHEUS_MULTIPROC_DIR` (set in `fly.toml`,
  created by `docker-entrypoint.sh`) collects per-process files that `/metrics` aggregates.
- The `[[metrics]]` block in `fly.toml` is what makes Fly's managed Prometheus scrape every
  machine. **Without it, none of these metrics are collected** — so if you add metrics,
  confirm that block still covers the process that emits them.

`prometheus_client` is an optional dependency: if it's absent every metric call is a no-op,
so the app still runs (handy for local dev). Sentry (optional, `observability.py`) handles
error monitoring.

---

*Historical note: this platform was previously branded **CallSight**; it is now **LINDA**.
Residual `callsight-*` identifiers only survive where they are load-bearing migration bridges
(e.g. the localStorage key rename) — treat any other occurrence as cleanup.*
