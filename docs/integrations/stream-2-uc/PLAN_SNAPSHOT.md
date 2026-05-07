# Plan — Eight Phone-System Integrations, Four Parallel Streams

## Context

LINDA today integrates with Twilio, SignalWire, and Telnyx as CPaaS adapters for live audio capture. The product positioning calls for "telephony-agnostic" support across the most popular phone systems — Cisco (CUCM/CUBE), Webex Calling, Microsoft Teams, Zoom Phone, Avaya (Aura/SBCE), Genesys Cloud, RingCentral, and Metaswitch. Building eight integrations in parallel inside one Claude instance is not feasible: each is a distinct workstream with its own protocol, certification, and dependency graph. This plan splits the work across four autonomous Claude instances such that each can run independently without overwriting the others, plus identifies what only the human user can do.

The eight providers fall into four architectural patterns:
- **SIPREC ingestion** (Cisco CUBE, Avaya SBCE, Metaswitch Perimeta) — RFC 7866 standard; one infrastructure, three vendor-specific SBC config templates.
- **UC vendor API + OAuth + webhook** (RingCentral, Webex Calling, Zoom Phone) — three OAuth clients with similar lifecycle (subscribe → webhook → fetch recording → dispatch transcription).
- **Microsoft Teams compliance recording** — the outlier; gated behind Microsoft certification, requires a .NET stateful media bot in Azure. Re-bounded to scaffolding only this round.
- **Genesys AudioHook** — WebSocket-based real-time audio streaming; AppFoundry partnership for production distribution.

Intended outcome: each stream's Claude instance lands a complete, test-covered, append-only patch set against synthetic fixtures and stops at the user-only line. The user then runs real-vendor verification, registers the developer apps, and signs the partner agreements.

---

## Critical Files (Reference)

| Path | Purpose |
|---|---|
| `backend/app/api/oauth.py` | Central OAuth registry. `CRM_PROVIDERS` dict at line 79 (renamed to `OAUTH_PROVIDERS` by Stream 2). |
| `backend/app/api/telephony.py` | Existing Twilio/SignalWire/Telnyx routes. **Frozen for this work** — new providers get their own api files. |
| `backend/app/main.py` | FastAPI app entry. Each stream adds one `app.include_router(...)` inside a marked region. |
| `backend/app/models.py` | SQLAlchemy models. `Tenant` line 35, `LiveSession` line 736 (read-only for all streams), `Integration` line 1050. New session/recording models appended at file tail per stream region. |
| `backend/app/services/telephony/__init__.py` | Holds the typed-Literal `Integration.provider` namespace contract (added in Stream 0). |
| `backend/app/services/meeting_scheduler/base.py` | Reference ABC pattern (`MeetingProvider`) for new adapter base classes. |
| `backend/app/services/crm/sync_service.py` | Reference `_build_adapter()` + `on_token_refresh` callback pattern. |
| `backend/app/services/email_ingest/push.py` | Reference push-webhook pattern (Pub/Sub / Graph). |
| `backend/app/services/token_crypto.py` | AES-256 helpers `encrypt_token()` / `decrypt_token()`. |
| `fly.toml` | Process list (api, worker, beat). Stream 1 adds `siprec_srs`. |
| `requirements.txt` | Append-only per stream. |

---

## Stream 0 — Pre-Work (must land before any of 1–4 forks)

A single human-in-the-loop pass that lands the shared dependencies all four streams need. **Owner: orchestrator (human or single Claude instance, not one of the four).**

### S0.1 Marker regions in shared files

Land empty marker comments in the three coordinated-shared files so subsequent streams append cleanly:

`backend/app/main.py`:
```python
# === BEGIN MULTI-STREAM ROUTER REGION (do not edit other streams' lines) ===
# stream-1/siprec:
# stream-2/uc:
# stream-3/teams:
# stream-4/audiohook:
# === END MULTI-STREAM ROUTER REGION ===
```

`requirements.txt`:
```
# === BEGIN MULTI-STREAM DEPS REGION ===
# stream-1/siprec:
# stream-2/uc:
# stream-3/teams:
# stream-4/audiohook:
# === END MULTI-STREAM DEPS REGION ===
```

`backend/app/models.py` (at the file tail):
```python
# === BEGIN MULTI-STREAM MODELS REGION ===
# stream-1/siprec:
# stream-2/uc:
# stream-3/teams:
# stream-4/audiohook:
# === END MULTI-STREAM MODELS REGION ===
```

### S0.2 Audio format normalizer

New file `backend/app/services/audio/normalizer.py` exposing:

```python
class AudioFormat(str, Enum):
    MULAW_8K = "mulaw_8k"
    PCM16_8K = "pcm16_8k"
    PCM16_16K = "pcm16_16k"
    PCM16_24K = "pcm16_24k"
    PCM16_48K = "pcm16_48k"
    OPUS_16K = "opus_16k"
    OPUS_48K = "opus_48k"
    MP3 = "mp3"
    WAV = "wav"
    FLAC = "flac"

def to_mulaw_8k(data: bytes, src_format: AudioFormat) -> bytes: ...
def to_pcm16_8k(data: bytes, src_format: AudioFormat) -> bytes: ...
def detect_format(data: bytes, hint: Optional[str] = None) -> AudioFormat: ...
```

Implementation: use `audioop` for μ-law/PCM conversions (stdlib, no deps), `pydub` for MP3/WAV decoding (already a likely dependency via Whisper), `opuslib` for Opus. The transcription pipeline downstream expects μ-law 8 kHz (Deepgram-streaming format) or PCM 16 kHz (Whisper batch). Frozen after Stream 0 lands; no stream modifies it.

### S0.3 Provider namespace contract

Append to `backend/app/services/telephony/__init__.py`:

```python
from typing import Literal

# Reserved Integration.provider strings for telephony providers.
# Adding a new value? Coordinate via the plan doc — collisions are a runtime bug.
TelephonyProvider = Literal[
    # Existing CPaaS:
    "twilio", "signalwire", "telnyx",
    # Stream 1 — SIPREC:
    "siprec_cisco_cube", "siprec_avaya_sbce", "siprec_metaswitch",
    # Stream 2 — UC vendor API:
    "ringcentral", "webex_calling", "zoom_phone",
    # Stream 3 — Teams compliance:
    "teams_compliance",
    # Stream 4 — Genesys AudioHook:
    "genesys_audiohook",
]
```

### S0.4 Branch & PR convention

- Long-lived branch `integrations-trunk` cut from `main`.
- Each stream branches `stream-N/<feature>` from `integrations-trunk`.
- PRs base off `integrations-trunk`. After all four streams merge there, one promotion PR to `main`.
- Alembic revision prefixes (per stream): `siprec_*`, `uc_*`, `teams_*`, `audiohook_*`.

### S0.5 Plan-doc update protocol

Each stream's Claude instance must, before exiting:
1. Update its stream's "Status" line in this plan doc (✅ landed / 🟡 in-progress / ❌ blocked-on-user).
2. Append a "Lessons / divergences" line under its stream block if anything material changed from the plan.
3. Commit the plan-doc update in its PR.

---

## Stream 1 — SIPREC (Cisco CUBE / Avaya SBCE / Metaswitch Perimeta)

**Status: ✅ landed (2026-05-07) on branch `stream-1/siprec`.**

**Lessons / divergences:**
- Worked in an isolated git worktree at
  `.claude/worktrees/stream-1-siprec/` rather than the shared main
  worktree — multiple streams were modifying the shared tree
  concurrently and the marker-region edits collided. Branching to a
  worktree gave a clean checkout of `stream-1/siprec` that wasn't
  contaminated by Streams 2/3/4's in-flight uncommitted changes.
- No new Python deps were needed. Multipart MIME, SDP, and rs-metadata
  XML parsing all use the stdlib (`xml.etree`, `re`, `audioop`,
  `base64`); the SRS terminates SRTP, so the audio path doesn't need a
  Python crypto lib.
- DTLS-SRTP key extraction is **not** in `siprec/srtp.py` — the SRS
  handles DTLS termination itself. The module documents this; only
  SDES suites are validated. DTLS becomes a v2 feature when we move
  SRTP into Python (we won't, for performance).
- The Dockerfile defaults to FreeSWITCH 1.10 from Debian's package
  archive when no SignalWire token is supplied. Production users are
  pointed at the SignalWire packages via `--secret id=fs_token` —
  documented in `docs/integrations/stream-1-siprec/USER_TODO.md`.
- The `fly.toml` `siprec_srs` process is declared, but Fly uses one
  Dockerfile per app by default. The user must build + push the SRS
  image separately and deploy with `--process-group siprec_srs --image
  ...` (USER_TODO §6).
- 60 tests pass (`tests/test_siprec_protocol.py` +
  `test_siprec_bridge.py` + the existing `test_audio_normalizer.py`);
  the docker-compose smoke test is gated behind `SIPREC_INTEGRATION=1`
  because Docker isn't in the default CI runner.

### Scope

Stand up an internally-hosted **Session Recording Server (SRS)** that speaks SIPREC (RFC 7866) and forwards audio frames into LINDA's transcription pipeline. The customer's SBC is the **Session Recording Client (SRC)**; LINDA's SRS is the receiver. Three vendors share the same protocol; per-vendor differences are SBC config + SRTP key exchange (SDES vs DTLS-SRTP) only.

### Files owned (CREATE only)

- `backend/app/services/telephony/siprec/__init__.py`
- `backend/app/services/telephony/siprec/bridge.py` — Python service that listens on a UNIX socket / TCP port for the SRS sidecar's media frames + control events, and pumps audio into transcription.
- `backend/app/services/telephony/siprec/protocol.py` — multipart-SDP parser, metadata XML parser (rs-metadata namespace).
- `backend/app/services/telephony/siprec/srtp.py` — SDES key extraction (DTLS-SRTP punted to v2).
- `backend/app/api/siprec.py` — REST endpoints `POST /siprec/events` (SRS → backend recording-started/stopped) and `POST /admin/integrations/siprec` (tenant config: SBC IP allowlist, shared secret).
- `backend/app/services/telephony/siprec_srs/Dockerfile` — sidecar container based on `drachtio/drachtio-siprec-recording-server` image.
- `backend/app/services/telephony/siprec_srs/config.json.template` — tenant-aware drachtio config.
- `tests/test_siprec_protocol.py` — synthetic SIPREC INVITE parsing.
- `tests/test_siprec_bridge.py` — bridge with mocked transcription dispatch.
- `tests/fixtures/siprec/` — sipp scenario files for synthetic INVITEs (in/out RTP streams + multipart metadata).
- `docs/telephony/siprec/cisco_cube.md` — Cisco IOS-XE 17+ CUBE config: `media-recording`, `media profile recorder`, `voice class sip-options-keepalive`.
- `docs/telephony/siprec/avaya_sbce.md` — Avaya SBCE 8/10 config: Recording Profile, Media Forking, SIPREC server entry, mTLS trust.
- `docs/telephony/siprec/metaswitch_perimeta.md` — Metaswitch CFS v9.0.10+ config + Perimeta SBC SIP proxy rules.
- `docs/integrations/stream-1-siprec/USER_TODO.md` — user-only checklist.
- Alembic migration: `siprec_001_initial.py`.

### Shared files appended (with markers only)

- `backend/app/main.py` — `app.include_router(siprec_router, prefix="/api/v1")` in the `# stream-1/siprec:` line.
- `requirements.txt` — `pyOpenSSL`, `aiortp`, optionally `pjsua2` (decide; can punt). Add under `# stream-1/siprec:`.
- `backend/app/models.py` — append `SiprecSession` model in `# stream-1/siprec:` block.
- `fly.toml` — Stream 1 owns this. Add `[[processes]]` for `siprec_srs` running the drachtio sidecar; allocate a Fly machine sized for media (consider `performance-2x`); document UDP-port range exposure.

### Forbidden files (MUST NOT touch)

- Any other stream's directory under `services/telephony/uc/`, `services/teams_recording/`, `api/uc_telephony.py`, `api/teams_recording.py`, `api/audiohook.py`.
- `backend/app/services/telephony/twilio.py`, `signalwire.py`, `telnyx.py`.
- `LiveSession` model (read-only).

### Reserved namespaces

- `Integration.provider` values: `siprec_cisco_cube`, `siprec_avaya_sbce`, `siprec_metaswitch`.
- Env vars: `SIPREC_SRS_HOST`, `SIPREC_SRS_PORT`, `SIPREC_SRS_SHARED_SECRET`, `SIPREC_FLY_PRIVATE_IP`.
- Alembic revision prefix: `siprec_*`.

### Implementation steps

1. Implement `siprec/protocol.py` for multipart-SDP and rs-metadata parsing (RFC 7865 metadata format). Reference: drachtio-siprec source.
2. Build `siprec_srs/Dockerfile` based on `drachtio/drachtio-siprec-recording-server:latest` with rtpengine OR FreeSWITCH backend (decision: FreeSWITCH userspace because rtpengine kernel deps don't run on Fly — see risks).
3. `siprec/bridge.py`: WebSocket or named-pipe consumer of the SRS's audio stream output. Normalize via `services/audio/normalizer.py`, pump into existing transcription dispatch.
4. `api/siprec.py`: REST endpoints to receive SRS lifecycle events (`recording.started` / `recording.stopped`) and create/finalize `SiprecSession` rows (sibling to `LiveSession`).
5. `SiprecSession` model: `id`, `tenant_id`, `live_session_id` FK, `src_call_id`, `src_metadata` JSONB, `started_at`, `ended_at`, `is_consent_attested` bool.
6. Tenant config endpoint `POST /admin/integrations/siprec`: stores SBC IP allowlist, shared secret, SRTP profile in `Integration.provider_config`.
7. Per-vendor config docs (three .md files): exact CLI/UI steps for Cisco CUBE, Avaya SBCE, Metaswitch CFS.
8. Synthetic-fixture tests using sipp: spin up the SRS sidecar in CI via docker-compose, fire a sipp scenario, assert that `SiprecSession` is created and audio frames reach transcription.

### User-only items (cannot be automated)

- Provision a public IP and DNS hostname for the SRS (or a dedicated hostname on Fly Edge).
- Issue and install TLS certificates for SIP-TLS and mTLS trust with each customer's SBC (Cisco/Avaya often require mTLS).
- Decide rtpengine vs FreeSWITCH userspace — rtpengine needs kernel iptables, won't work on Fly. Recommendation: FreeSWITCH; flag CPU-per-call cost.
- Per-customer onboarding: provide the SRS hostname, port, shared secret, and SBC config snippet to the customer's network team. SBC config is vendor-specific and lives on the customer's side.
- Optionally: contract a SaaS SRS provider (Voxida, RecordingService) instead of self-hosting; if so, the bridge becomes a thin webhook consumer.
- Procure a Fly machine size capable of media processing (likely off-Fly host for production scale).

### Acceptance criteria (autonomous-stop line)

- `pytest tests/test_siprec_protocol.py tests/test_siprec_bridge.py` passes.
- `docker-compose up siprec_srs` boots the sidecar; sipp scenario delivers a synthetic INVITE; one `SiprecSession` row is written; one `LiveSession` row is updated with `source="siprec_cisco_cube"`.
- Three vendor docs render with copy-pasteable config blocks.
- `docs/integrations/stream-1-siprec/USER_TODO.md` exists and lists every user-only item above.

### Work-package prompt for Stream 1's Claude instance

> You are autonomous Claude Stream 1 of 4. Your scope is SIPREC ingestion for Cisco CUBE, Avaya SBCE, and Metaswitch Perimeta. Read `/Users/davisfox/.claude/plans/fair-pushback-let-s-create-playful-puddle.md` first; the "Stream 1" section is your contract. You may CREATE files only in your owned-files list. You may APPEND to shared files only inside the `# stream-1/siprec:` marker line. You may NOT touch other streams' files or `LiveSession`. Build to the acceptance criteria, then update the plan-doc Status line to ✅ landed (or 🟡 with a blocker note) and add a "Lessons / divergences" line. Open a PR against `integrations-trunk`. Stop at the user-only line — do not attempt real SBC interop or TLS provisioning. If you discover a hard blocker not anticipated in the plan, write it to your USER_TODO.md and stop.

---

## Stream 2 — UC Vendor API (RingCentral / Webex Calling / Zoom Phone)

**Status: not started.**

### Scope

OAuth-based integration with three UC providers that expose recording-completion webhooks plus REST endpoints to fetch the recorded audio. All three follow the same lifecycle: install → OAuth consent → subscribe to events → receive `recording.completed` webhook → fetch audio → normalize → dispatch transcription.

### Files owned (CREATE only)

- `backend/app/services/telephony/uc/__init__.py`
- `backend/app/services/telephony/uc/base.py` — `UCRecordingProvider(ABC)` with `subscribe(tenant)`, `verify_webhook(req)`, `fetch_recording(call_id) -> bytes + AudioFormat`.
- `backend/app/services/telephony/uc/ringcentral.py` — Subscription API, Call Log API recording fetch.
- `backend/app/services/telephony/uc/webex.py` — Webhooks API + Converged Recordings API (PKCE OAuth).
- `backend/app/services/telephony/uc/zoom_phone.py` — `phone.recording_completed` webhook + recording fetch.
- `backend/app/services/telephony/uc/fetch_task.py` — Celery task `fetch_uc_recording(provider, tenant_id, call_id)` that downloads, normalizes, and dispatches to transcription.
- `backend/app/api/uc_telephony.py` — webhook handlers `POST /uc/ringcentral/webhook`, `POST /uc/webex/webhook`, `POST /uc/zoom/webhook`. Each verifies signature, idempotency-checks against `UcRecordingJob`, enqueues fetch.
- `tests/test_uc_ringcentral.py`, `tests/test_uc_webex.py`, `tests/test_uc_zoom_phone.py` — pure unit tests with `respx` or `httpx_mock` fixtures of real recorded API responses.
- `tests/fixtures/uc/` — recorded webhook payloads + recording-fetch HTTP responses per vendor.
- `docs/integrations/stream-2-uc/RingCentral.md`, `Webex.md`, `ZoomPhone.md` — admin install guides + scope lists per vendor.
- `docs/integrations/stream-2-uc/USER_TODO.md`.
- Alembic migration: `uc_001_uc_recording_job.py`.

### Shared files appended (with markers only)

- `backend/app/main.py` — `app.include_router(uc_telephony_router, prefix="/api/v1")` in `# stream-2/uc:`.
- `requirements.txt` — pin `respx` if needed (most deps already present). Add under `# stream-2/uc:`.
- `backend/app/models.py` — append `UcRecordingJob` model in `# stream-2/uc:` block (idempotency table: `provider`, `external_call_id`, `state`, `attempts`, `interaction_id` FK).

### Shared files Stream 2 owns outright (single-owner edits)

- `backend/app/api/oauth.py` — Stream 2 owns this for the duration. Two changes:
  1. Rename `CRM_PROVIDERS` dict to `OAUTH_PROVIDERS` and add a backward-compat alias `CRM_PROVIDERS = OAUTH_PROVIDERS` immediately below the new name. Update internal references in this file. Importing modules elsewhere keep working through the alias.
  2. Add three new entries: `ringcentral`, `webex_calling`, `zoom_phone`.

Example new entries:
```python
"ringcentral": {
    "authorize_url": "https://platform.ringcentral.com/restapi/oauth/authorize",
    "token_url": "https://platform.ringcentral.com/restapi/oauth/token",
    "scopes": ["ReadAccounts", "ReadCallLog", "ReadCallRecording", "Subscriptions"],
    "scope_sep": " ",
    "client_id_key": "RINGCENTRAL_CLIENT_ID",
    "client_secret_key": "RINGCENTRAL_CLIENT_SECRET",
    "certified": False,
},
"webex_calling": {
    "authorize_url": "https://webexapis.com/v1/authorize",
    "token_url": "https://webexapis.com/v1/access_token",
    "scopes": [
        "spark:calls_read", "spark:recordings_read",
        "spark-admin:recordings_read", "spark-admin:telephony_config_read",
    ],
    "scope_sep": " ",
    "client_id_key": "WEBEX_CLIENT_ID",
    "client_secret_key": "WEBEX_CLIENT_SECRET",
    "use_pkce": True,
    "certified": False,
},
"zoom_phone": {
    "authorize_url": "https://zoom.us/oauth/authorize",
    "token_url": "https://zoom.us/oauth/token",
    "scopes": ["phone:read:admin", "phone_recording:read:admin", "phone_call_log:read:admin"],
    "scope_sep": " ",
    "client_id_key": "ZOOM_PHONE_CLIENT_ID",
    "client_secret_key": "ZOOM_PHONE_CLIENT_SECRET",
    "certified": False,
},
```

### Forbidden files (MUST NOT touch)

- `backend/app/services/telephony/twilio.py`, `signalwire.py`, `telnyx.py`, `siprec/*`, `teams_recording/*`, `audiohook.py`.
- `LiveSession` model.
- Other streams' marker regions in shared files.

### Reserved namespaces

- `Integration.provider`: `ringcentral`, `webex_calling`, `zoom_phone`.
- Env vars: `RINGCENTRAL_CLIENT_ID`, `RINGCENTRAL_CLIENT_SECRET`, `WEBEX_CLIENT_ID`, `WEBEX_CLIENT_SECRET`, `ZOOM_PHONE_CLIENT_ID`, `ZOOM_PHONE_CLIENT_SECRET`, plus per-provider webhook signing keys.
- Alembic revision prefix: `uc_*`.

### Implementation steps

1. Define `UCRecordingProvider` ABC mirroring `MeetingProvider` shape (see `services/meeting_scheduler/base.py`).
2. Implement RingCentral: OAuth (existing oauth.py flow), Subscription API to register webhook delivery for `/restapi/v1.0/account/~/extension/~/telephony/sessions` events, signature verification (RingCentral validation token + JWT), recording fetch from Call Log API URL.
3. Implement Webex: PKCE OAuth (extension to oauth.py — most providers don't use PKCE today), Webhooks API for `recordings:created`, signature verification (X-Spark-Signature HMAC-SHA1), recording fetch via Converged Recordings API.
4. Implement Zoom Phone: OAuth (similar to existing Zoom meetings entry), Event Subscriptions for `phone.recording_completed`, verification token + secret-token URL validation flow, recording fetch via download URL.
5. Webhook idempotency: every webhook creates/updates a `UcRecordingJob` keyed on `(provider, external_call_id)`. Late-arriving duplicates are no-ops.
6. Celery `fetch_uc_recording` task: download bytes, detect format via `services/audio/normalizer.py`, write to audio storage (`s3_audio.py`), create `Interaction` row, dispatch to existing transcription pipeline.
7. Admin UI endpoint: `POST /admin/integrations/uc/{provider}/connect` initiates OAuth.
8. Synthetic-fixture tests: each provider gets a recorded webhook payload (in `tests/fixtures/uc/`) and a recorded API response for the recording fetch. Test the full webhook → idempotency → fetch → dispatch path with `respx`.

### User-only items (cannot be automated)

- Register developer apps:
  - **RingCentral**: developer.ringcentral.com → create app → set scopes → request graduation to production.
  - **Webex**: developer.webex.com → create integration → register redirect URI → request App Hub listing for distribution.
  - **Zoom**: marketplace.zoom.us → create OAuth app → set Phone scopes → submit for marketplace review (weeks).
- Provide test accounts with paid licenses (Zoom Phone test license is licensed, RingCentral developer sandbox is free, Webex developer free tier is limited).
- Submit each app for production / marketplace approval before any real customer can install.
- Webhook URLs: provide the public callback URLs (LINDA's prod hostname + `/uc/{provider}/webhook`) to each provider's app config.

### Acceptance criteria

- `pytest tests/test_uc_*.py` passes against fixtures.
- OAuth providers list at `GET /api/v1/oauth/providers` shows the three new entries.
- For each provider, replaying a recorded webhook in CI creates a `UcRecordingJob` row, the Celery task fetches the fixture audio, an `Interaction` is created, and a transcription job is enqueued.
- Three admin docs exist with explicit scope lists and redirect-URI setup steps.
- `docs/integrations/stream-2-uc/USER_TODO.md` lists every developer-portal action.

### Work-package prompt for Stream 2's Claude instance

> You are autonomous Claude Stream 2 of 4. Your scope is OAuth-based UC vendor recording integration for RingCentral, Webex Calling, and Zoom Phone. Read `/Users/davisfox/.claude/plans/fair-pushback-let-s-create-playful-puddle.md`; the "Stream 2" section is your contract. You own `backend/app/api/oauth.py` for this work — do the `CRM_PROVIDERS` → `OAUTH_PROVIDERS` rename with back-compat alias, then add the three new entries. You may CREATE files in your owned list and APPEND inside `# stream-2/uc:` markers. Build to the acceptance criteria using recorded fixtures (`respx` / `httpx_mock`); do NOT make live API calls. Update the plan-doc Status line and add a "Lessons / divergences" entry. Open a PR against `integrations-trunk`. Stop at the user-only line — production marketplace approvals and live-token testing are user tasks.

### Lessons / divergences (Stream 2)

- **PKCE became a shared concern.** Webex Calling requires PKCE on the
  authorize-code flow. Rather than fork the catalog code-flow URL
  builder per provider, added a generic `use_pkce` flag that the
  builder honours: stash a fresh `code_verifier` on the state Redis
  payload at authorize time, emit the SHA256 `code_challenge` on the
  authorize URL, send the verifier on the token exchange. RingCentral
  and Zoom Phone don't set the flag, so the path is unchanged for
  them. This is a small extension to the OAuth registry that future
  providers (e.g. Microsoft public-client flows) can also use.
- **`CRM_PROVIDERS` rename kept as alias.** Per the plan, renamed to
  `OAUTH_PROVIDERS` and added `CRM_PROVIDERS = OAUTH_PROVIDERS` as a
  back-compat alias. Two existing tests (`test_audit_nice_to_haves.py`,
  `test_meeting_scheduler.py`) still import the old name; they keep
  working through the alias and the alias is kept permanent.
- **Webhook secret stored on Integration.provider_config rather than
  encrypted column.** `Integration.access_token` / `refresh_token` are
  Fernet-encrypted, but `provider_config` is plaintext JSONB. The
  vendor webhook secrets (RC verification token, Webex `secret`, Zoom
  Secret Token) are sensitivity-equivalent to a vendor API key, so
  storing them on `provider_config["webhook_secret"]` was fine for
  this round; if the threat model later distinguishes API keys from
  webhook secrets, this is the spot to encrypt.
- **Webex content-type doesn't always say MP3.** Webex commonly
  delivers MP4 audio. The normalizer's pydub path handles it via
  ffmpeg, but the format-hint logic in `webex.py` returns `None` for
  MP4 rather than coercing to WAV (which would corrupt downstream
  decode). Documented in `Webex.md` — operator must confirm ffmpeg is
  on the worker host.
- **Subscription creation/renewal is user-managed, not LINDA-managed.**
  After OAuth completes, the operator currently creates the RC /
  Webex subscriptions manually with a per-tenant secret and persists
  it via `POST /admin/integrations/uc/{provider}/webhook-secret`.
  Phase 2 (post-MVP) is to have LINDA create + auto-renew these
  subscriptions itself; tracked in `USER_TODO.md`.
- **Migration prefix uses underscore, not hyphen.** Existing migrations
  use lower-case prefix-then-underscore (e.g. `siprec_001_*.py`); the
  alembic CI guard the plan describes was inferred — confirmed by
  checking sibling migrations.

---

## Stream 3 — Microsoft Teams Compliance Recording (scaffolding only)

**Status: ✅ scaffold landed (2026-05-07) on branch `stream-3/teams-scaffold`. Full .NET media bot remains deferred per the original re-bounding.**

### Lessons / divergences (Stream 3)

- The scaffold endpoint cannot validate the per-subscription `clientState`
  yet — we have the parser primitive but no persistence layer for the
  secret. Endpoint passes `expected_client_state=None`; the follow-on
  workstream wires this when subscriptions become first-class rows.
- `TeamsCallRecord` table is created empty. Without the media bot we
  have no useful state to write per Graph notification, so the
  notification handler logs and returns 202 instead of inserting
  rows. Schema is in place so the bot follow-on doesn't need a
  migration.
- The unit-test suite mounts the router on a fresh `FastAPI()` rather
  than booting `main.app`, because `main.app`'s lifespan probes the DB.
  This matches the pre-existing pattern for non-DB router tests.
- `create_subscription` (Graph HTTP call) is implemented but not
  exercised in CI. The scaffold has no production path that calls it
  yet — the renewal scheduler is part of the follow-on, and adding a
  mocked HTTP test for an unused code path was deemed lower-value than
  pinning the parser/handshake contracts that the follow-on will
  depend on. If the follow-on changes the URL or body shape, that's
  fine — it'll write its own test then.

### Scope

Per user's explicit choice, this round produces only the **Python control plane**: Graph subscription handling, the bot-callback HTTP endpoints, the policy-state model, and a stub bot interface. The actual .NET media bot, Azure infra, and Microsoft Teams Compliance Recording certification are **out of scope** for this round and become a separate workstream when the user is ready to commit.

### Files owned (CREATE only)

- `backend/app/services/teams_recording/__init__.py`
- `backend/app/services/teams_recording/graph_app_auth.py` — App-only Microsoft Graph auth (client credentials flow, MSAL). Stores app-permission token separately from user OAuth.
- `backend/app/services/teams_recording/subscriptions.py` — manages `/communications/onlineMeetings/getAllRecordings` and call-record subscriptions.
- `backend/app/services/teams_recording/bot_interface.py` — abstract `MediaBot` interface that the (future) .NET bot will implement. Stub implementation that no-ops and logs a "media bot not deployed" warning.
- `backend/app/services/teams_recording/policy.py` — placeholder for `New-CsTeamsComplianceRecordingApplication` PowerShell template generation.
- `backend/app/api/teams_recording.py` — `POST /teams/notification` (Graph change notification webhook with validation token), `POST /teams/bot/callback` (placeholder for .NET bot callbacks).
- `tests/test_teams_subscriptions.py`, `tests/test_teams_notification_validation.py`.
- `tests/fixtures/teams/` — sample Graph notification payloads, validation handshake payloads.
- `docs/integrations/stream-3-teams/CERTIFICATION_PATH.md` — what the certification process requires; what we have today (Python scaffold) vs what's needed (the .NET media bot).
- `docs/integrations/stream-3-teams/USER_TODO.md`.
- Alembic migration: `teams_001_teams_call_record.py`.

### Shared files appended

- `backend/app/main.py` — `# stream-3/teams:` includes `teams_recording_router`.
- `requirements.txt` — `msal` (MSAL Python for app-only auth). Add under `# stream-3/teams:`.
- `backend/app/models.py` — `TeamsCallRecord` model in `# stream-3/teams:` block.

### Forbidden files

- All other streams' files. `oauth.py` (Stream 2 owns it; Teams uses app-only auth via the new `graph_app_auth.py`, NOT the user-OAuth registry). `LiveSession` model.

### Reserved namespaces

- `Integration.provider`: `teams_compliance`.
- Env vars: `TEAMS_BOT_APP_ID`, `TEAMS_BOT_APP_SECRET`, `TEAMS_TENANT_ID` (per-customer in `Integration.provider_config`), `TEAMS_GRAPH_NOTIFICATION_URL`.
- Alembic revision prefix: `teams_*`.

### Implementation steps

1. MSAL app-only auth: `graph_app_auth.py` issues `https://graph.microsoft.com/.default` tokens.
2. Subscription manager: subscribe to `/communications/callRecords` and `/users/{tenantUserId}/onlineMeetings/getAllRecordings` change notifications. Implement validation-token handshake (`POST /teams/notification?validationToken=...` returns 200 with token).
3. `TeamsCallRecord` model: tenant_id, call_id, organizer, participant list (JSONB), join_url, recording_url, certification_status (enum: scaffold | bot_required | recording_fetched).
4. Stub `MediaBot` interface — methods like `attach_to_call(call_id)`, `detach(call_id)`, `is_available()`. Stub returns "not deployed" in all environments. Future .NET implementation will call back into `/teams/bot/callback`.
5. `CERTIFICATION_PATH.md`: Microsoft Partner registration, Azure tenant setup, app registration with `Calls.AccessMedia.All` + `Calls.JoinGroupCallAsGuest.All`, .NET media bot from microsoft-graph-comms-samples, certification timeline (3-9 months), `New-CsTeamsComplianceRecordingApplication` PowerShell setup.
6. Synthetic-fixture tests: validate the notification handshake, test subscription renewal logic, test `MediaBot` stub returns "not deployed".

### User-only items

- **Microsoft certification track** (gated, months-long): partner registration, attestation, certification submission.
- **Azure subscription** for the bot app registration and (future) bot hosting.
- **App registration** in Azure AD with `Calls.AccessMedia.All`, `Calls.JoinGroupCallAsGuest.All`, `OnlineMeetingArtifact.Read.All` permissions; admin consent.
- **Customer-side**: tenant admin must run `New-CsTeamsComplianceRecordingApplication` PowerShell, assign the bot app to a Teams compliance recording policy, and assign that policy to recorded users.
- **Decision**: when ready, commission the .NET media bot OR investigate Azure Communication Services Call Recording as the media surface (separate workstream).

### Acceptance criteria

- `pytest tests/test_teams_*.py` passes.
- Validation-token handshake against a synthetic Graph notification payload returns 200 with the token.
- `MediaBot` stub instantiates and reports "not deployed".
- `CERTIFICATION_PATH.md` documents the full certification process and is reviewed by the user.
- `USER_TODO.md` enumerates every Microsoft-side action.

### Work-package prompt for Stream 3's Claude instance

> You are autonomous Claude Stream 3 of 4. Your scope is **Python control-plane scaffolding only** for Microsoft Teams Compliance Recording. The full .NET media bot and Microsoft certification are explicitly OUT OF SCOPE this round per the user's decision. Read `/Users/davisfox/.claude/plans/fair-pushback-let-s-create-playful-puddle.md`; the "Stream 3" section is your contract. You may CREATE files in your owned list and APPEND inside `# stream-3/teams:` markers. Do NOT touch `oauth.py` — Teams uses app-only Graph auth in your own `graph_app_auth.py`. Build the subscription handling, validation handshake, model, stub bot interface, and certification documentation. Update the plan-doc Status line and add a "Lessons / divergences" entry. Open a PR against `integrations-trunk`. Stop at the user-only line — do not attempt Azure deployment or .NET work.

---

## Stream 4 — Genesys Cloud AudioHook

**Status: not started.**

### Scope

Implement a WebSocket server that speaks the Genesys AudioHook Protocol and consumes real-time audio from Genesys Cloud's AudioHook Monitor. Frames are forwarded to the existing transcription pipeline. The integration is distributed via Genesys AppFoundry; sandbox usage is possible without that.

### Files owned (CREATE only)

- `backend/app/services/telephony/audiohook/__init__.py`
- `backend/app/services/telephony/audiohook/protocol.py` — AudioHook frame parser (binary message types: open, opened, ping, pong, audio, close, closed, etc.). Spec at developer.genesys.cloud/devapps/audiohook/.
- `backend/app/services/telephony/audiohook/server.py` — WebSocket session handler: probe response, open negotiation, audio dispatch.
- `backend/app/services/telephony/audiohook/auth.py` — request signature verification (HMAC-SHA256 over canonical headers; Genesys signs with the integration's client secret).
- `backend/app/api/audiohook.py` — `WebSocket /audiohook/{tenant_id}` endpoint.
- `tests/test_audiohook_protocol.py`, `tests/test_audiohook_server.py`, `tests/test_audiohook_auth.py`.
- `tests/fixtures/audiohook/` — recorded session traces (open / audio / close sequences).
- `docs/integrations/stream-4-audiohook/Genesys.md` — admin steps to configure AudioHook Monitor in Genesys Cloud, integration credentials, channel selection (agent, customer, both).
- `docs/integrations/stream-4-audiohook/USER_TODO.md`.
- Alembic migration: `audiohook_001_audiohook_session.py`.

### Shared files appended

- `backend/app/main.py` — WebSocket route registration in `# stream-4/audiohook:`.
- `requirements.txt` — likely no new deps (FastAPI provides WebSocket primitives). Mark slot reserved.
- `backend/app/models.py` — `AudiohookSession` model in `# stream-4/audiohook:` block.

### Forbidden files

- All other streams' files. `oauth.py` (AudioHook auth is HMAC, not OAuth — config goes on `Integration.provider_config`). `LiveSession` (use sibling `AudiohookSession`).

### Reserved namespaces

- `Integration.provider`: `genesys_audiohook`.
- Env vars: `AUDIOHOOK_DEFAULT_AUDIO_FORMAT` (PCMU vs L16). Per-tenant: integration credentials in `Integration.provider_config`.
- Alembic revision prefix: `audiohook_*`.

### Implementation steps

1. Read the AudioHook Protocol spec (developer.genesys.cloud/devapps/audiohook/) and implement the frame parser. Handle: text-frame JSON control messages + binary-frame audio.
2. Probe handshake: when Genesys connects, the server must answer specific probe messages or the integration won't enable.
3. Open negotiation: client (Genesys) proposes audio format (PCMU/L16, sample rate, channels); server picks one and confirms.
4. Audio routing: each audio frame includes a sequence number and audio bytes. Decode via `services/audio/normalizer.py` to μ-law 8kHz, dispatch to existing live-transcription pipeline.
5. Heartbeats: implement ping/pong every 10s, and pause/resume on customer-initiated PCI pause (`paused` / `resumed` control messages).
6. `AudiohookSession` model: tenant_id, conversation_id, participant_id, started_at, ended_at, channel ("agent" | "customer" | "both"), is_consent_attested.
7. Auth: each WebSocket connection includes signed headers; verify HMAC-SHA256 against the per-tenant secret stored on `Integration.provider_config`.
8. Synthetic-fixture tests: replay recorded probe + open + audio + close sequences against the server using a WebSocket test client.

### User-only items

- Create a Genesys Cloud developer org (sandbox).
- Submit AppFoundry partnership application + listing for production distribution.
- Per-customer admin: enable AudioHook Monitor integration in Genesys Cloud admin → Integrations → AudioHook Monitor → enter integration credentials → choose channels (agent/customer/both) → activate.
- Provide the public WebSocket URL (`wss://<host>/api/v1/audiohook/{tenant_id}`) to each customer for their integration config.
- Note: AudioHook is metered per-minute by Genesys; user must accept and budget for that cost.

### Acceptance criteria

- `pytest tests/test_audiohook_*.py` passes.
- Replaying a recorded session in CI: the server completes probe + open, accepts audio frames, handles a pause/resume cycle, and writes one `AudiohookSession` row with the right channel field.
- HMAC verification rejects unsigned and tampered payloads.
- `Genesys.md` lists every admin step with screenshots-as-text.

### Work-package prompt for Stream 4's Claude instance

> You are autonomous Claude Stream 4 of 4. Your scope is the Genesys Cloud AudioHook WebSocket integration. Read `/Users/davisfox/.claude/plans/fair-pushback-let-s-create-playful-puddle.md`; the "Stream 4" section is your contract. The Genesys-published AudioHook Protocol spec is your source of truth — implement the probe handshake, open negotiation, ping/pong, pause/resume, and audio routing. You may CREATE files in your owned list and APPEND inside `# stream-4/audiohook:` markers. Build to the acceptance criteria using recorded session fixtures; do NOT connect to a real Genesys org. Update the plan-doc Status line and add a "Lessons / divergences" entry. Open a PR against `integrations-trunk`. Stop at the user-only line — AppFoundry partnership and customer-side AudioHook activation are user tasks.

### Lessons / divergences (Stream 4)

- **Auth implementation chose RFC 9421.** The plan said "HMAC-SHA256 over canonical headers"; the AudioHook spec uses the IETF HTTP Message Signatures RFC 9421 vocabulary (Signature-Input + Signature, with `keyid`/`alg`/`created`/`nonce`). Implemented to that spec rather than a Cavage-draft canonical-string scheme. Required-component enforcement is locked to `@request-target`, `@authority`, `audiohook-organization-id`, `audiohook-session-id`, `audiohook-correlation-id`, `x-api-key`. See `services/telephony/audiohook/auth.py`.
- **Replay-protection nonce cache deferred.** Auth accepts any nonce within a 600s `created` skew window. A Redis-backed nonce TTL is a follow-up; documented in `USER_TODO.md`. Reasoning: AppFoundry review tests for replay tolerance, and tightening pre-listing risks false rejections.
- **Production secret-storage shape kept flexible.** The route reads `client_secret` either from `Integration.access_token` (encrypted via `services.token_crypto`) OR from `provider_config["client_secret"]`. The admin CRUD endpoint that persists this row is out of scope — accepting both shapes lets the follow-up choose without re-migrating.
- **Sink kept transport-agnostic.** `services/telephony/audiohook/server.py` defines an `AudioSink` Protocol; the production sink in `api/audiohook.py` (`_LiveTranscriptionSink`) wires Deepgram live, but the state machine is exercised in tests with a list-appending sink. No DB / WebSocket / Deepgram dependency in the unit-test path.
- **Did not modify `LiveSession`.** Per ownership rules, AudioHook sessions are tracked in a sibling `AudiohookSession` table only. Cross-linking AudioHook sessions to LiveSession (when an agent has both a CPaaS leg and an AudioHook leg simultaneously) is a future workstream.
- **Migration depends on `z3b4c5d6e7f8`.** If Streams 1–3 land migrations against the same down-revision, integrators will need `alembic merge` (already called out in the plan's Verification section).
- **Spec-vs-real-world risk surfaced in `USER_TODO.md`.** Four assumptions (signature components list, L16 byte order, probe message shape, `paused`/`resumed` direction) need verification against a live Genesys sandbox before AppFoundry submission. Listed under "Spec assumptions worth verifying."

---

## Coordination Protocol — Rules All Four Instances Follow

1. **Read this plan first.** Every instance starts by reading the full plan file and its own stream's section.
2. **Never edit another stream's lines.** Marker regions in shared files (`main.py`, `requirements.txt`, `models.py`) are line-blocked by stream. The instance's only valid edit in those files is *its own* marker line.
3. **Never modify `LiveSession`.** Sibling models only (`SiprecSession`, `UcRecordingJob`, `TeamsCallRecord`, `AudiohookSession`).
4. **Never edit `services/telephony/twilio.py`, `signalwire.py`, `telnyx.py`, or existing `api/telephony.py` routes.** Frozen for this work.
5. **Never edit another stream's owned directory or api file.**
6. **Use the typed-Literal `TelephonyProvider`** in `services/telephony/__init__.py` for all `Integration.provider` writes. If a value isn't in the Literal, the type checker rejects it.
7. **Alembic revision prefix is mandatory.** A revision file without the right prefix fails CI.
8. **Branch convention:** `stream-N/<feature>`, base off `integrations-trunk`, PR to `integrations-trunk` (not `main`).
9. **Synthetic-fixture acceptance only.** No live-vendor calls. If you can't test it with fixtures, document it in `USER_TODO.md` and stop.
10. **Update this plan doc before exiting.** Every stream's commit must update its Status line and add "Lessons / divergences" if anything material diverged.
11. **Surface unanswered questions.** If you encounter a decision the user hasn't authorized — new dependencies, new infra, new env vars — write it to your `USER_TODO.md` rather than picking silently.

---

## Verification (end-to-end, after all streams land)

1. `git checkout integrations-trunk && pytest -q` — all 4 streams' synthetic-fixture tests green together.
2. `alembic upgrade head` — applies all 4 migration prefixes cleanly with `alembic merge` if revision graph branched.
3. `python -c "from backend.app.main import app"` — startup smoke-imports the app with all 4 routers registered.
4. `docker-compose up siprec_srs` — the SRS sidecar boots; sipp scenario from `tests/fixtures/siprec/` produces an end-to-end SIPREC → transcription dispatch trace.
5. Replay each UC vendor's recorded webhook against the local API; confirm `UcRecordingJob` is created and the Celery fetch task runs with the fixture audio response.
6. WebSocket-test-client replays an AudioHook session against the server; confirm `AudiohookSession` row + transcription dispatch.
7. Validate Graph notification handshake against a synthetic payload; confirm `TeamsCallRecord` is *not* created (because the bot is a stub) but the subscription state is recorded correctly.
8. Open a PR from `integrations-trunk` to `main` only after every stream's Status is ✅ landed.

After this verification, the user runs the live-vendor verification per each stream's `USER_TODO.md` (developer-portal registrations, certification submissions, customer onboarding configs).

---

## User-Only Master Checklist (cross-stream)

The following items can never be performed by an autonomous Claude instance and are blockers for production rollout:

| Stream | User-only action |
|---|---|
| 1 | Public IP / DNS for SRS; TLS certificates; rtpengine vs FreeSWITCH decision; Fly vs non-Fly host decision; per-customer SBC config coordination. |
| 2 | RingCentral developer-portal app graduation; Webex Developer + App Hub listing; Zoom App Marketplace review; test accounts with paid licenses; per-customer admin OAuth consent. |
| 3 | Microsoft Partner registration; Azure subscription; Teams Compliance Recording certification application; commission the .NET media bot (separate workstream); customer-side `New-CsTeamsComplianceRecordingApplication` PowerShell. |
| 4 | Genesys Cloud developer org; AppFoundry partnership + listing; per-customer AudioHook Monitor activation; budget for per-minute metered usage. |

This list is the truth-source for "what's left after Claude finishes." Each stream's `docs/integrations/stream-N-*/USER_TODO.md` mirrors its row.

---

## Status Tracking

| Stream | Status | Owner | Lessons / divergences |
|---|---|---|---|
| 0 (pre-work) | ⏳ not started | orchestrator | — |
| 1 (SIPREC) | ✅ landed | stream-1/siprec | FreeSWITCH userspace SRS sidecar (rtpengine deferred to user); DTLS-SRTP termination handled by SRS, not Python; no new Python deps. See "Lessons / divergences" above. |
| 2 (UC API) | ✅ landed | stream-2/uc-vendor-api | OAuth registry renamed `CRM_PROVIDERS` → `OAUTH_PROVIDERS` with permanent back-compat alias; minimal PKCE support added (Webex). See "Lessons / divergences (Stream 2)" below. |
| 3 (Teams scaffold) | ✅ landed | stream-3/teams-scaffold | scaffold-only round; full .NET media bot remains a separate workstream. See "Lessons / divergences (Stream 3)" above. |
| 4 (AudioHook) | ✅ landed | stream-4/audiohook | HMAC verification implemented as RFC 9421 (HTTP Message Signatures) with HMAC-SHA256; `LiveSession` left untouched per ownership rules. See "Lessons / divergences (Stream 4)" below. |

Each stream's instance updates its row before exiting.
