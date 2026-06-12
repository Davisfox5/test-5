# Session handoff — production readiness pass

**Date**: 2026-04-25
**Branch**: `claude/audit-production-readiness-T0EML` (merged to `main`)
**Latest commit on main**: `04c3acc`

This document is the conversation context distilled. A new Claude Code
session can read this top-to-bottom and pick up where the previous one
ended without any other context.

---

## The arc

The session was an end-to-end production-readiness pass on LINDA — a
multi-tenant voice/email/chat analysis platform. We shipped, in order:

1. **Speaker diarization completion**. Whisper now runs `pyannote.audio`
   and merges turns into segments by max-overlap. Deepgram already had
   it; we added a URL-mode path so external recording systems can hand
   us a pointer instead of bytes.
2. **Full transcription pipeline**. The Celery voice task was a
   placeholder; it now handles three real paths (pre-populated
   transcript, S3-staged upload, external `audio_url`) and cleans up
   audio after analysis.
3. **Telephony refactor**. Removed outbound dial, hold, transfer,
   warm-transfer, conference-join, recording callback, and the entire
   `CallRecording` model + retention sweeper. Audio is no longer
   stored long-term anywhere — it's transcribed and discarded. Live
   Media Streams (Twilio / SignalWire / Telnyx) stayed in scope.
4. **External-recording ingest**. New `POST /interactions/ingest-recording`
   accepts either a JSON URL pointer (passed straight to Deepgram) or
   a multipart upload (briefly staged in S3, then deleted).
5. **KB auto-sync**. Five providers shipped: Google Drive/Docs,
   OneDrive/SharePoint, Confluence, generic API push, MCP server pull.
   Wired into the existing embedding pipeline + Celery.
6. **Profile RBAC**. Agents see their own + customers they've talked
   to; managers see their reports; admins see everything; business
   profile is admin-only. Applied across `/profiles/*` and
   `/interactions/{id}/scores`.
7. **Paralinguistics — complete**. Finished the extractor (speaking
   rate via de Jong & Wempe, pause rate via Praat silence TextGrid).
   Wired post-call to run on every voice interaction (S3 staging or
   URL fetch). Built `LiveParalinguisticWindow` for real-time, fed by
   Deepgram's live diarization stream — slices per speaker for
   real-time per-speaker features. Added `ParalinguisticScanner`
   (monotone / pace / stress / silence). Wired into churn/sentiment
   scorers via `customer_arousal`, `customer_hot_voice`,
   `agent_voice_stress`, `agent_monotone`. Tenant baselines compute
   nightly.
8. **Arousal classifier**. Deterministic 0..1 score from existing
   acoustic features (no new dep). Annotates every paralinguistic
   block in post-call and live.
9. **Emotion classifier (optional)**. SpeechBrain wav2vec2 IEMOCAP
   gated behind a tenant feature flag; pre-warm helper on worker
   start; cache dir configurable.
10. **CRM full parity** — Pipedrive, HubSpot, Salesforce all read
    deals + write notes + create tasks + update deal stage + map
    custom fields by hash. Webhooks (Pipedrive). Write-back routes
    by contact's `crm_source`.
11. **Audio codec independence**. Replaced stdlib `audioop` (removed
    in 3.13) with a pure-Python G.711 μ-law decoder + linear-interp
    resampler. Python 3.13 migration is now just a runtime bump.
12. **Production readiness**:
    - Health (liveness) + readiness probes (Postgres/Redis hard;
      Qdrant/Deepgram/S3 soft).
    - Structured JSON logs + ContextVar-based correlation IDs
      (`request_id`, `tenant_id`, `interaction_id`, …).
    - Sentry SDK with PII scrubbing + tenant tags.
    - Celery `worker_process_init` warmup for pyannote + SpeechBrain.
    - GDPR Articles 15 / 17 / 20: streaming export + hard delete with
      audit log table.
    - Prometheus metrics: pipeline stages, transcription, Celery,
      CRM, telephony, paralinguistic snapshots; queue-depth sampler
      every 30 s.
    - Feature-flag admin UI in `apps/app` wired to
      `/admin/tenant-settings`.
    - Paralinguistic corpus harness with F1 gate (`--min-f1` for CI).
    - Live-paralinguistic soak-test script.
    - Nightly per-tenant S3 backup + restore Celery tasks.
    - End-to-end smoke harness (`scripts/smoke.py`).
    - Operator runbook (`docs/runbook.md`) and DR plan
      (`docs/multi_region_dr.md`).
    - CI/CD with build + deploy_staging + deploy_production jobs.
    - `.env.example` + `docs/deployment.md` configuration guide.

## What's deferred (and why)

- **Multi-region**. Documented in `docs/multi_region_dr.md`; not
  implemented. Trigger: first EU-residency customer or AWS us-east-1
  outage > 4 h.
- **Non-English paralinguistic tuning**. Out of scope per the user.
- **Custom-field-mapping admin UI** for CRM write-back. Adapters
  support it via `provider_config.field_map`; the UI to set those
  hashes hasn't been built. Blocks a feature, not a deploy.
- **Production load test of live paralinguistic at peak concurrency**.
  Harness exists (`scripts/loadtest_live_paralinguistic.py`); the run
  itself needs a staging box and a ground-truth WAV.
- **Smoke against real Deepgram URL-mode + HubSpot/Salesforce sandbox**.
  Harness exists (`scripts/smoke.py`); needs sandbox credentials to run
  for real.

## What's blocked on user input

User is procuring deploy secrets. The exact list is in
`docs/deployment.md` — the minimum-staging checklist is 9 provider
keys + 4 runtime URLs:

1. Anthropic API key
2. Deepgram API key
3. Hugging Face read token (must accept pyannote model licence)
4. Voyage AI API key
5. Clerk secret + publishable key
6. Sentry DSN
7. AWS access key + secret + S3 bucket
8. Postgres connection string
9. Redis connection string

Plus three host/registry decisions:
- container registry (GHCR vs. ECR vs. …)
- API host (Fly / Render / ECS / k8s)
- staging domain

Once those land, the next session should:
- Help fill in `.env.staging` (in chat, not committed)
- Produce the `gh` CLI commands or web-UI walkthrough for setting
  GitHub Actions secrets + variables.
- Run the smoke harness against staging + report what's broken.

## Architecture decisions worth knowing about

- **No long-term audio storage anywhere.** Audio exists in S3 staging
  for the seconds-to-minutes between upload and transcription, then
  gets deleted. URL-mode ingest never touches our storage at all
  (Deepgram fetches directly).
- **Track inference is dead.** Twilio/SignalWire/Telnyx `track`
  metadata (inbound/outbound) is *not* used as a proxy for
  agent/customer. Speaker assignment comes from Deepgram live
  diarization timeline only.
- **CRM write-back routing.** `_pick_provider_for_writeback` in
  `services/crm/writeback.py` picks the destination based on
  `tenant.branding_config.crm_writeback_provider` (override) →
  `Contact.crm_source` → `Customer.crm_source` → most recent
  Integration row.
- **Worker warmup.** `worker_process_init` signal preloads pyannote
  (~500 MB) and SpeechBrain (~1 GB). Gated by `LINDA_WORKER_WARMUP`;
  beat-only workers should set it to `0`.
- **Token encryption.** Fernet with a `TOKEN_ENCRYPTION_KEY` env var.
  Two-key rotation via `TOKEN_ENCRYPTION_KEYS_FALLBACK` documented in
  `docs/runbook.md` §5.
- **GDPR audit log.** `tenant_dataops_log` table is intentionally
  *not* tenant-cascaded; rows survive after the tenant is hard-
  deleted so a regulator review still has the trail.

## Test posture + env quirks

- Total: 86 unit tests passing locally for everything we shipped
  (paralinguistics + CRM write-back + corpus + adapter).
- 1 skipped: parselmouth-required end-to-end test for the extractor.
- 2 skipped without model: SpeechBrain emotion live tests.
  Set `LINDA_EMOTION_TESTS_DISABLED=1` to keep CI fast.
- The dev sandbox couldn't reach Hugging Face fast enough to actually
  download the SpeechBrain model end-to-end (~1 GB throttled). The
  code path is exercised through model discovery; production workers
  with normal internet will complete the download on first
  `prefetch_emotion_classifier()` call.
- Some tests' transitive imports pull in torch (~3.5 GB). Running
  `tests/test_paralinguistics_emotion_live.py` in a fresh shell can
  appear to hang during torch initialization. Skip that file in CI
  (the workflow already does).

## Pointers to the new code

If you're picking this up, these are the files most likely to come
up in a follow-up question:

- `backend/app/services/transcription.py` — TranscriptionService with
  metric wrapping, Deepgram + Whisper paths, pyannote merge.
- `backend/app/services/paralinguistics_live.py` — `LiveParalinguisticWindow`
  with `update_diarization()` and per-speaker slicing.
- `backend/app/services/paralinguistics_replay.py` — replay harness
  + ground-truth comparator.
- `backend/app/services/paralinguistics_corpus.py` — manifest-driven
  F1 gate.
- `backend/app/services/crm/writeback.py` — provider routing.
- `backend/app/services/crm/{pipedrive,hubspot,salesforce}.py` — full
  CRUD + write-back per provider.
- `backend/app/services/tenant_dataops.py` — GDPR export/delete.
- `backend/app/services/audio_codecs.py` — pure-Python μ-law +
  resample (the audioop replacement).
- `backend/app/logging_setup.py` — JSON formatter + context vars.
- `backend/app/observability.py` — Sentry init.
- `backend/app/api/health.py` — `/health` + `/ready` + `/ready/deep`.
- `backend/app/api/gdpr.py` — `/tenants/{id}/export` + DELETE.
- `backend/app/api/telephony.py` — Media Streams handlers, paralinguistic
  publish, CRM webhook.
- `backend/app/services/metrics.py` — every Prometheus metric.
- `backend/app/tasks.py` — Celery tasks + signals + beat schedule
  (search `tenant_export_to_s3`, `crm_writeback`, `sample_queue_depth`,
  `worker_process_init`).
- `apps/app/src/lib/tenant-settings.ts` — feature-flag hook.
- `apps/app/src/app/(app)/settings/page.tsx` — feature-flag UI.
- `scripts/smoke.py`, `scripts/loadtest_live_paralinguistic.py` —
  ops scripts.
- `corpora/example.yaml`, `corpora/README.md` — corpus format.
- `docs/runbook.md`, `docs/deployment.md`, `docs/multi_region_dr.md`,
  `.env.example` — operator-facing.

## Migrations added this session

- `g4b5c6d7e8f9_drop_call_recordings_add_audio_url`
- `h5c6d7e8f9a0_paralinguistic_baselines`
- `i6d7e8f9a0b1_crm_deals_and_pipedrive_fields`
- `j7e8f9a0b1c2_tenant_dataops_log`

## Outstanding follow-ups for the next session

In priority order:

1. **User is configuring deploy secrets.** When they come back with
   provider keys + host decision, walk them through filling
   `.env.staging` and setting GitHub secrets. Run smoke against the
   staging URL.
2. **Annotate the paralinguistic corpus.** 20–50 minutes of audio,
   labeled per `corpora/README.md`. Then run
   `python -m backend.app.services.paralinguistics_corpus … --min-f1 0.70`
   and treat the result as the gate for flipping the
   `paralinguistic_live` tenant flag.
3. **Production load-test live paralinguistics.** Run
   `scripts/loadtest_live_paralinguistic.py` at 1 / 10 / 25 / 50
   concurrent calls on a prod-sized box; record `cpu_mean_pct` and
   `snapshot_ms_p99`; pick the per-box concurrency cap from real
   numbers.
4. **First nightly backup validation.** After the first scheduled
   `tenant_backup_all_tenants` run, verify the bundle decompresses
   and that `tenant_restore_from_s3` reproduces the source on a
   throwaway DB.
5. **Custom-field mapping admin UI** if a real CRM tenant turns up
   with custom fields they want LINDA to write to.

## What NOT to redo

- Don't re-add audioop. The pure-Python decoder is the canonical path
  now; importing audioop will break on Python 3.13.
- Don't add track-based speaker mapping back to live paralinguistics.
  Diarization is the abstraction; provider track metadata is
  unreliable across warm transfers + conferences.
- Don't add a new GDPR export endpoint; it exists. If you need a
  variant (e.g. CSV format), extend the existing one.
- Don't store call recordings long-term. Product policy is
  transcribe-and-discard. The `CallRecording` model + retention
  sweeper were deliberately removed.
