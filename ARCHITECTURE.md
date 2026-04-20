# LINDA — Call Transcription & AI Insights Platform

## Architecture & Implementation Plan

### Product Vision

A white-label SaaS platform that ingests audio from customer calls (phone, VoIP, video conferencing), produces real-time and batch transcriptions, and uses AI to generate actionable next steps, follow-ups, sentiment analysis, and coaching suggestions. Sold B2B to sales and customer-support organizations.

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENT TIER                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────────┐ │
│  │  React SPA   │  │  Mobile App  │  │  Embeddable Widget (SDK)  │ │
│  │  (Dashboard) │  │  (React Nat) │  │  (white-label iframe)     │ │
│  └──────┬───────┘  └──────┬───────┘  └───────────┬───────────────┘ │
└─────────┼─────────────────┼──────────────────────┼─────────────────┘
          │                 │                      │
          ▼                 ▼                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       API GATEWAY (Kong / AWS API Gateway)          │
│       Rate limiting · JWT auth · Tenant routing · API versioning    │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────────┐
│  Auth Service    │ │  Core API        │ │  Webhook / Integration   │
│  (FastAPI)       │ │  (FastAPI)       │ │  Service (FastAPI)       │
│  - OAuth2/OIDC   │ │  - Calls CRUD    │ │  - Salesforce            │
│  - RBAC          │ │  - Transcripts   │ │  - HubSpot              │
│  - Tenant mgmt   │ │  - AI Insights   │ │  - Slack / Teams        │
│  - API keys      │ │  - Search        │ │  - Zapier               │
└────────┬─────────┘ └────────┬─────────┘ └────────┬─────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     MESSAGE BUS (Redis Streams / Kafka)             │
│         call.uploaded · transcription.complete · ai.analyzed        │
└──────────┬──────────────────┬──────────────────┬────────────────────┘
           ▼                  ▼                  ▼
┌────────────────────┐ ┌───────────────┐ ┌───────────────────────────┐
│  Transcription     │ │  AI Analysis  │ │  Notification Worker      │
│  Worker            │ │  Worker       │ │  - Email digests          │
│  - Whisper / Deepg │ │  - Claude API │ │  - Slack/Teams alerts     │
│  - Diarization     │ │  - Summaries  │ │  - CRM push              │
│  - Chunked stream  │ │  - Action items│ │                          │
└────────┬───────────┘ └───────┬───────┘ └───────────────────────────┘
         │                     │
         ▼                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         DATA TIER                                   │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────────┐ │
│  │  PostgreSQL   │  │  S3 / Blob    │  │  Elasticsearch           │ │
│  │  (multi-ten)  │  │  (audio files)│  │  (transcript search)     │ │
│  └──────────────┘  └───────────────┘  └──────────────────────────┘ │
│  ┌──────────────┐  ┌───────────────┐                               │
│  │  Redis        │  │  ClickHouse   │                               │
│  │  (cache/queue)│  │  (analytics)  │                               │
│  └──────────────┘  └───────────────┘                               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Project Structure

```
linda/
├── docker-compose.yml              # Local dev orchestration
├── docker-compose.prod.yml         # Production overrides
├── Makefile                        # Common dev commands
│
├── backend/
│   ├── alembic/                    # DB migrations
│   │   └── versions/
│   ├── app/
│   │   ├── main.py                 # FastAPI entry point
│   │   ├── config.py               # Pydantic settings (env-based)
│   │   ├── database.py             # SQLAlchemy async engine + session
│   │   ├── middleware/
│   │   │   ├── tenant.py           # Tenant isolation middleware
│   │   │   ├── auth.py             # JWT validation middleware
│   │   │   └── rate_limit.py       # Per-tenant rate limiting
│   │   ├── models/
│   │   │   ├── tenant.py           # Tenant, Plan, Subscription
│   │   │   ├── user.py             # User, Role, Permission
│   │   │   ├── call.py             # Call, CallRecording
│   │   │   ├── transcript.py       # Transcript, TranscriptSegment
│   │   │   └── insight.py          # AIInsight, ActionItem, FollowUp
│   │   ├── schemas/                # Pydantic request/response models
│   │   │   ├── call.py
│   │   │   ├── transcript.py
│   │   │   ├── insight.py
│   │   │   └── auth.py
│   │   ├── api/
│   │   │   ├── v1/
│   │   │   │   ├── router.py       # Aggregated v1 router
│   │   │   │   ├── calls.py        # POST /calls, GET /calls/:id
│   │   │   │   ├── transcripts.py  # GET /transcripts/:id
│   │   │   │   ├── insights.py     # GET /insights/:call_id
│   │   │   │   ├── auth.py         # Login, register, API keys
│   │   │   │   ├── tenants.py      # Tenant admin endpoints
│   │   │   │   ├── webhooks.py     # Incoming telephony webhooks
│   │   │   │   └── search.py       # Full-text transcript search
│   │   │   └── deps.py             # Dependency injection (db, user, tenant)
│   │   ├── services/
│   │   │   ├── transcription.py    # Orchestrates ASR pipeline
│   │   │   ├── ai_analysis.py      # Claude API integration
│   │   │   ├── call_ingest.py      # Audio upload + validation
│   │   │   ├── storage.py          # S3 abstraction
│   │   │   └── integrations/
│   │   │       ├── salesforce.py
│   │   │       ├── hubspot.py
│   │   │       └── slack.py
│   │   └── workers/
│   │       ├── transcription_worker.py   # Async transcription job
│   │       ├── ai_analysis_worker.py     # Async AI analysis job
│   │       └── notification_worker.py    # Async notification dispatch
│   ├── tests/
│   │   ├── conftest.py
│   │   ├── test_calls.py
│   │   ├── test_transcription.py
│   │   ├── test_ai_analysis.py
│   │   └── test_auth.py
│   ├── requirements.txt
│   ├── Dockerfile
│   └── alembic.ini
│
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── main.tsx
│   │   ├── api/                    # Generated API client (openapi-ts)
│   │   ├── components/
│   │   │   ├── layout/
│   │   │   │   ├── Sidebar.tsx
│   │   │   │   ├── Header.tsx
│   │   │   │   └── TenantSwitcher.tsx
│   │   │   ├── calls/
│   │   │   │   ├── CallList.tsx
│   │   │   │   ├── CallDetail.tsx
│   │   │   │   ├── CallUpload.tsx
│   │   │   │   └── CallPlayer.tsx  # Audio player + synced transcript
│   │   │   ├── transcripts/
│   │   │   │   ├── TranscriptView.tsx
│   │   │   │   ├── SpeakerTimeline.tsx
│   │   │   │   └── SearchHighlight.tsx
│   │   │   ├── insights/
│   │   │   │   ├── InsightPanel.tsx
│   │   │   │   ├── ActionItems.tsx
│   │   │   │   ├── SentimentChart.tsx
│   │   │   │   └── CoachingNotes.tsx
│   │   │   └── dashboard/
│   │   │       ├── Overview.tsx     # KPIs, recent calls
│   │   │       ├── Analytics.tsx    # Trends, team performance
│   │   │       └── TeamView.tsx
│   │   ├── hooks/
│   │   │   ├── useAuth.ts
│   │   │   ├── useCalls.ts
│   │   │   └── useWebSocket.ts     # Live transcription updates
│   │   ├── stores/                  # Zustand state management
│   │   │   ├── authStore.ts
│   │   │   └── callStore.ts
│   │   └── styles/
│   │       └── theme.ts            # White-label theming tokens
│   ├── package.json
│   ├── vite.config.ts
│   ├── tailwind.config.ts
│   └── Dockerfile
│
└── infra/
    ├── terraform/
    │   ├── modules/
    │   │   ├── ecs/                 # Container orchestration
    │   │   ├── rds/                 # PostgreSQL
    │   │   ├── s3/                  # Audio storage
    │   │   ├── elasticache/         # Redis
    │   │   └── elasticsearch/
    │   ├── environments/
    │   │   ├── staging/
    │   │   └── production/
    │   └── main.tf
    ├── k8s/                         # Alternative K8s manifests
    │   ├── base/
    │   └── overlays/
    └── github-actions/
        ├── ci.yml
        ├── deploy-staging.yml
        └── deploy-production.yml
```

---

## 3. Database Schema (Core Tables)

```sql
-- Multi-tenant isolation via tenant_id on every table
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    slug            VARCHAR(63) UNIQUE NOT NULL,
    plan            VARCHAR(50) DEFAULT 'starter',
    branding_config JSONB DEFAULT '{}',    -- logo, colors, domain
    settings        JSONB DEFAULT '{}',    -- feature flags, limits
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    email       VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255),
    full_name   VARCHAR(255),
    role        VARCHAR(50) DEFAULT 'member',  -- admin, manager, member
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE TABLE api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash    VARCHAR(255) NOT NULL,
    name        VARCHAR(255),
    scopes      TEXT[] DEFAULT '{}',
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE calls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
    uploaded_by     UUID REFERENCES users(id),
    external_id     VARCHAR(255),           -- CRM reference
    title           VARCHAR(500),
    call_type       VARCHAR(50),            -- sales, support, onboarding
    participants    JSONB DEFAULT '[]',
    duration_secs   INTEGER,
    audio_url       TEXT NOT NULL,           -- S3 presigned path
    audio_format    VARCHAR(20),            -- wav, mp3, ogg, webm
    sample_rate     INTEGER,
    status          VARCHAR(50) DEFAULT 'uploaded',
    -- status flow: uploaded → transcribing → transcribed → analyzing → complete → error
    source          VARCHAR(100),           -- twilio, zoom, upload, api
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE transcripts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id     UUID REFERENCES calls(id) ON DELETE CASCADE,
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    language    VARCHAR(10) DEFAULT 'en',
    full_text   TEXT,
    word_count  INTEGER,
    confidence  FLOAT,
    engine      VARCHAR(50),                -- whisper, deepgram, assembly
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE transcript_segments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transcript_id   UUID REFERENCES transcripts(id) ON DELETE CASCADE,
    speaker_id      VARCHAR(100),
    speaker_name    VARCHAR(255),
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    text            TEXT NOT NULL,
    confidence      FLOAT,
    sentiment       VARCHAR(20),            -- positive, neutral, negative
    seq_order       INTEGER NOT NULL
);

CREATE TABLE ai_insights (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id     UUID REFERENCES calls(id) ON DELETE CASCADE,
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    model       VARCHAR(100),               -- claude-sonnet-4-6, etc.
    summary     TEXT,
    sentiment_overall VARCHAR(20),
    sentiment_score   FLOAT,
    topics      JSONB DEFAULT '[]',
    key_moments JSONB DEFAULT '[]',         -- [{time_ms, description, type}]
    coaching    JSONB DEFAULT '{}',
    raw_response JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE action_items (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    insight_id  UUID REFERENCES ai_insights(id) ON DELETE CASCADE,
    call_id     UUID REFERENCES calls(id) ON DELETE CASCADE,
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    assigned_to UUID REFERENCES users(id),
    title       VARCHAR(500) NOT NULL,
    description TEXT,
    priority    VARCHAR(20) DEFAULT 'medium', -- high, medium, low
    due_date    DATE,
    status      VARCHAR(50) DEFAULT 'pending', -- pending, in_progress, done, dismissed
    category    VARCHAR(100),               -- follow_up, send_info, schedule_meeting, escalate
    source_segment_id UUID REFERENCES transcript_segments(id),
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Indexes for performance
CREATE INDEX idx_calls_tenant ON calls(tenant_id, created_at DESC);
CREATE INDEX idx_calls_status ON calls(tenant_id, status);
CREATE INDEX idx_segments_transcript ON transcript_segments(transcript_id, seq_order);
CREATE INDEX idx_action_items_assignee ON action_items(assigned_to, status);
CREATE INDEX idx_action_items_tenant ON action_items(tenant_id, status, created_at DESC);
```

---

## 4. Core AI Pipeline

### 4.1 Transcription Pipeline

```
Audio Upload → Validate & Normalize (ffmpeg) → Chunk if >30min
    → Send to ASR Engine (Whisper large-v3 / Deepgram Nova-2)
    → Speaker Diarization (pyannote-audio)
    → Merge diarization + transcription
    → Store segments in DB + full text in Elasticsearch
```

**Engine Strategy (pluggable):**
| Tier | Engine | Use Case |
|------|--------|----------|
| Real-time | Deepgram streaming | Live call monitoring |
| Batch (default) | Whisper large-v3 (self-hosted) | Cost-efficient batch processing |
| Batch (premium) | Deepgram Nova-2 / AssemblyAI | Higher accuracy, faster turnaround |

### 4.2 AI Analysis Pipeline (Claude)

After transcription completes, the AI worker sends the transcript to Claude with a structured prompt:

```python
ANALYSIS_SYSTEM_PROMPT = """You are an expert sales and customer-support analyst.
Given a call transcript with speaker labels and timestamps, produce a structured
JSON analysis with the following sections:

1. **summary**: 2-3 sentence executive summary of the call
2. **sentiment**: overall sentiment (positive/neutral/negative) + score 0-1
3. **topics**: array of discussed topics with relevance scores
4. **key_moments**: critical moments in the call (objections, commitments, escalations)
5. **action_items**: concrete next steps extracted from the conversation, each with:
   - title, description, priority (high/medium/low), category, suggested_due_days
6. **coaching**: suggestions for the rep (what went well, what to improve)
7. **follow_up_email_draft**: a suggested follow-up email based on the conversation

Return valid JSON only."""
```

**Model Strategy:**
- Default: `claude-sonnet-4-6` — best cost/quality balance for batch analysis
- Premium tier: `claude-opus-4-6` — deeper coaching insights, complex calls
- Quick summaries: `claude-haiku-4-5` — for real-time sidebar suggestions

---

## 5. Implementation Phases

### Phase 1 — Foundation
| # | Task | Details |
|---|------|---------|
| 1.1 | Project scaffolding | FastAPI backend, React+Vite frontend, Docker Compose |
| 1.2 | Database & migrations | PostgreSQL + Alembic, core schema above |
| 1.3 | Auth system | JWT + refresh tokens, RBAC, tenant isolation middleware |
| 1.4 | Call upload API | `POST /api/v1/calls/upload` — multipart audio, S3 storage |
| 1.5 | Basic frontend | Login, dashboard shell, call list, upload UI |

### Phase 2 — Transcription Engine
| # | Task | Details |
|---|------|---------|
| 2.1 | Audio processing pipeline | ffmpeg normalization, format conversion, chunking |
| 2.2 | Whisper integration | Self-hosted Whisper large-v3, GPU worker |
| 2.3 | Speaker diarization | pyannote-audio integration, speaker label merging |
| 2.4 | Transcript storage | Segments in PG, full text in Elasticsearch |
| 2.5 | Transcript UI | Timeline view, speaker colors, audio sync playback |

### Phase 3 — AI Analysis
| # | Task | Details |
|---|------|---------|
| 3.1 | Claude API integration | Structured prompt, JSON parsing, retry logic |
| 3.2 | Action item extraction | Parse AI output → action_items table, assignment |
| 3.3 | Sentiment analysis | Per-segment + overall sentiment scoring |
| 3.4 | Insight dashboard | Summary cards, action items panel, sentiment chart |
| 3.5 | Follow-up email drafts | AI-generated email drafts from call context |

### Phase 4 — White-Label & Multi-Tenancy
| # | Task | Details |
|---|------|---------|
| 4.1 | Tenant provisioning | Self-serve signup, plan tiers, usage metering |
| 4.2 | White-label theming | Custom logos, colors, domains (CSS custom properties) |
| 4.3 | Custom domain support | Tenant CNAME mapping via Caddy/nginx + Let's Encrypt |
| 4.4 | API key management | Scoped API keys for programmatic access |
| 4.5 | Embeddable widget SDK | iframe-based + postMessage API for embedding |

### Phase 5 — Integrations & Real-Time
| # | Task | Details |
|---|------|---------|
| 5.1 | Telephony connectors | Twilio, Vonage, Zoom webhook ingestion |
| 5.2 | CRM sync | Salesforce, HubSpot — push action items + transcripts |
| 5.3 | Real-time transcription | WebSocket streaming, Deepgram live API |
| 5.4 | Notification system | Email digests, Slack/Teams alerts on call analysis |
| 5.5 | Zapier/webhook outbound | Event-driven outbound webhooks for custom integrations |

### Phase 6 — Analytics & Hardening
| # | Task | Details |
|---|------|---------|
| 6.1 | Team analytics | Rep performance, call volume trends, sentiment over time |
| 6.2 | Full-text search | Elasticsearch-powered transcript search with highlighting |
| 6.3 | Bulk operations | Batch upload, bulk re-analysis, CSV/PDF export |
| 6.4 | Admin portal | Super-admin for managing tenants, plans, feature flags |
| 6.5 | Audit logging | Immutable audit trail for compliance (SOC 2) |
| 6.6 | Load testing & hardening | k6 load tests, security audit, penetration testing |

---

## 6. Tech Stack Summary

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Backend API** | Python 3.12 + FastAPI | Async-native, auto OpenAPI docs, type-safe |
| **Task Queue** | Redis Streams + ARQ | Lightweight, Python-native async workers |
| **Database** | PostgreSQL 16 | JSONB, row-level security, mature ecosystem |
| **Search** | Elasticsearch 8 | Full-text transcript search with highlighting |
| **Object Storage** | S3 (or MinIO local) | Audio file storage with presigned URLs |
| **Cache** | Redis 7 | Session cache, rate limiting, pub/sub |
| **Frontend** | React 18 + TypeScript + Vite | Fast builds, type-safe, large ecosystem |
| **UI Framework** | Tailwind CSS + Radix UI | Themeable (white-label), accessible |
| **State Management** | Zustand + TanStack Query | Lightweight, server-state focused |
| **ASR (Speech-to-Text)** | Whisper large-v3 / Deepgram | Self-hosted for cost, Deepgram for speed |
| **Speaker Diarization** | pyannote-audio 3.x | Best open-source diarization |
| **AI Analysis** | Claude API (Anthropic) | Best-in-class reasoning for insights |
| **Infra** | Terraform + AWS ECS (or K8s) | Reproducible, scalable |
| **CI/CD** | GitHub Actions | Native integration |
| **Monitoring** | Prometheus + Grafana + Sentry | Metrics, alerting, error tracking |

---

## 7. White-Label Configuration Model

```jsonc
{
  "tenant_slug": "acme-corp",
  "branding": {
    "app_name": "Acme Call Intelligence",
    "logo_url": "https://cdn.acme.com/logo.svg",
    "favicon_url": "https://cdn.acme.com/favicon.ico",
    "primary_color": "#1E40AF",
    "secondary_color": "#3B82F6",
    "font_family": "Inter, sans-serif"
  },
  "domain": {
    "custom": "calls.acme.com",
    "default": "acme.linda.app"
  },
  "features": {
    "real_time_transcription": true,
    "crm_integrations": ["salesforce"],
    "max_call_duration_mins": 120,
    "max_monthly_minutes": 10000,
    "ai_model_tier": "premium",     // standard=haiku, default=sonnet, premium=opus
    "export_formats": ["csv", "pdf", "json"]
  }
}
```

---

## 8. Security & Compliance

- **Data Isolation**: Row-level security in PostgreSQL, tenant_id on every query
- **Encryption**: TLS in transit, AES-256 at rest (S3 SSE, RDS encryption)
- **Auth**: OAuth2 + OIDC, MFA support, scoped API keys with rotation
- **Audio Retention**: Configurable per-tenant retention policies with auto-deletion
- **Audit Logging**: Immutable append-only audit log for all data access
- **SOC 2 Type II**: Architecture designed for compliance from day one
- **GDPR**: Data export, right-to-deletion, processing agreements
- **PCI DSS**: No card data stored; billing via Stripe
- **Penetration Testing**: Quarterly third-party pen tests

---

## 9. Scaling Considerations

- **Transcription Workers**: Horizontally scalable GPU instances (ECS/K8s)
- **API Servers**: Stateless, scale behind ALB based on CPU/request count
- **Database**: Read replicas for analytics queries, connection pooling via PgBouncer
- **Audio Storage**: S3 with lifecycle policies (hot → warm → glacier)
- **Rate Limiting**: Per-tenant, per-endpoint via Redis sliding window
- **Cost Estimation** (at 10,000 calls/month, avg 15 min each):
  - Whisper GPU compute: ~$300/mo (spot instances)
  - Claude API: ~$500/mo (sonnet tier)
  - Infrastructure: ~$800/mo (RDS, ECS, S3, Redis, ES)
  - **Total: ~$1,600/mo** at moderate scale

---

## 10. API Design (Key Endpoints)

```
Authentication:
  POST   /api/v1/auth/login
  POST   /api/v1/auth/register
  POST   /api/v1/auth/refresh
  POST   /api/v1/auth/api-keys

Calls:
  POST   /api/v1/calls/upload          # Upload audio file
  POST   /api/v1/calls/ingest          # Webhook from telephony provider
  GET    /api/v1/calls                  # List calls (paginated, filtered)
  GET    /api/v1/calls/:id              # Call detail + status
  DELETE /api/v1/calls/:id              # Soft delete

Transcripts:
  GET    /api/v1/calls/:id/transcript   # Full transcript with segments
  GET    /api/v1/transcripts/search     # Full-text search across calls

Insights:
  GET    /api/v1/calls/:id/insights     # AI analysis for a call
  GET    /api/v1/calls/:id/action-items # Action items for a call
  PATCH  /api/v1/action-items/:id       # Update status/assignment
  GET    /api/v1/action-items           # All action items (filtered)

Analytics:
  GET    /api/v1/analytics/overview     # KPIs, call volume, sentiment
  GET    /api/v1/analytics/team         # Per-rep metrics
  GET    /api/v1/analytics/trends       # Time-series data

Tenant Admin:
  GET    /api/v1/admin/tenant           # Tenant settings
  PATCH  /api/v1/admin/tenant           # Update branding/settings
  GET    /api/v1/admin/users            # Manage users
  GET    /api/v1/admin/usage            # Usage & billing metrics

Webhooks:
  POST   /api/v1/webhooks/twilio        # Twilio call recording webhook
  POST   /api/v1/webhooks/zoom          # Zoom recording webhook
```
