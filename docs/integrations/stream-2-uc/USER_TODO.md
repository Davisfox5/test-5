# Stream 2 (UC vendor APIs) — user-only checklist

The autonomous Claude instance landed code + tests up to the synthetic-
fixture acceptance line. Everything below requires a developer-portal
login, marketplace submission, or live-vendor account, none of which an
autonomous agent can do safely.

## RingCentral

- [ ] Register a RingCentral developer-portal app at
      developer.ringcentral.com.
  - Auth type: Server (Authorization Code).
  - App permissions: `ReadAccounts`, `ReadCallLog`,
    `ReadCallRecording`, `Subscriptions`.
  - Redirect URI: `https://<linda-host>/api/v1/oauth/ringcentral/callback`.
- [ ] Set env vars: `RINGCENTRAL_CLIENT_ID`, `RINGCENTRAL_CLIENT_SECRET`.
- [ ] Graduate the app from sandbox to **Production** (sandbox apps
      can only be installed by accounts in your developer org).
- [ ] Decide whether sandbox-tenant flow is needed. If yes, document
      how tenants override `provider_config["api_base"]` to
      `https://platform.devtest.ringcentral.com`.
- [ ] Per-tenant operator step: when a customer connects, create the
      telephony-session subscription with a per-tenant verification
      token and persist that token via
      `POST /admin/integrations/uc/ringcentral/webhook-secret`.
- [ ] Build subscription-renewal automation. RC subscriptions expire
      after `expiresIn` seconds (we recommend 7776000 = 90 days);
      a scheduled task should renew via PUT before each expiry. Not
      yet implemented.

## Webex Calling

- [ ] Create a Webex Developer integration at developer.webex.com.
  - Scopes: `spark:calls_read`, `spark:recordings_read`,
    `spark-admin:recordings_read`,
    `spark-admin:telephony_config_read`.
  - Redirect URI: `https://<linda-host>/api/v1/oauth/webex_calling/callback`.
  - PKCE: enabled (S256). Already handled by oauth.py.
- [ ] Set env vars: `WEBEX_CLIENT_ID`, `WEBEX_CLIENT_SECRET`.
- [ ] Submit an **App Hub listing** for customer-installable
      distribution. Required for non-developer-org customers.
- [ ] Per-tenant operator step: after OAuth, create the recording
      webhook via `POST https://webexapis.com/v1/webhooks` (resource
      `recordings`, event `created`), then persist the secret with
      `POST /admin/integrations/uc/webex_calling/webhook-secret`.
- [ ] Confirm ffmpeg is on PATH for the LINDA worker — Webex commonly
      delivers MP4 audio, which pydub decodes via ffmpeg.

## Zoom Phone

- [ ] Create a separate Zoom marketplace OAuth app at marketplace.zoom.us
      (do NOT reuse the existing meetings app — different scope model).
  - Scopes: `phone:read:admin`, `phone_recording:read:admin`,
    `phone_call_log:read:admin`.
  - Redirect URI: `https://<linda-host>/api/v1/oauth/zoom_phone/callback`.
  - Event Subscriptions: enable `phone.recording_completed`.
  - Endpoint URL: `https://<linda-host>/api/v1/uc/zoom/webhook/<tenant-id>`.
- [ ] Set env vars: `ZOOM_PHONE_CLIENT_ID`, `ZOOM_PHONE_CLIENT_SECRET`.
- [ ] Submit for **marketplace review** (typically multiple weeks).
      Until approved, only accounts in your developer org can install.
- [ ] Per-tenant operator step: persist the marketplace Secret Token
      via `POST /admin/integrations/uc/zoom_phone/webhook-secret`.
- [ ] Confirm tenants connect with an account-level admin OAuth user
      (the `phone:*:admin` scopes don't work on user-level OAuth).

## Cross-cutting

- [ ] Wire the SPA admin UI to the `POST /admin/integrations/uc/{provider}/webhook-secret`
      endpoint. Today the secret has to be set via a curl call after
      the operator obtains it from the developer portal.
- [ ] Decide on the per-tenant subscription/renewal automation. The
      MVP just persists what the operator manually creates; the Phase
      2 enhancement is to have LINDA create the subscription itself
      after OAuth completes (RC and Webex both expose REST endpoints
      for this).
- [ ] Test against real-vendor accounts before a customer onboards.
      The respx-fixtured tests confirm the protocol shape; only a real
      RC sandbox / Webex developer org / Zoom Phone test license can
      confirm the end-to-end OAuth → webhook → fetch path against
      live infra. Budget for a Zoom Phone test license (paid).

## Migration step (when promoting from `integrations-trunk` to `main`)

- [ ] After all Stream 2..4 PRs land on `integrations-trunk`, run
      `alembic merge -m "merge stream migrations"` to fuse the parallel
      heads (`siprec_*`, `uc_*`, `teams_*`, `audiohook_*`) into one
      linear chain. Each stream's migration was deliberately branched
      from `z3b4c5d6e7f8` to keep the streams non-conflicting.
