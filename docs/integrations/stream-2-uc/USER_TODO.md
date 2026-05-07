# Stream 2 (UC vendor APIs) — user-only checklist

The autonomous Claude instance landed code + tests up to the synthetic-
fixture acceptance line. Everything below requires a developer-portal
login, marketplace submission, or live-vendor account, none of which an
autonomous agent can do safely.

## Vendor-wide signing secrets — ONE per vendor, set ONCE

The webhook signing secret is now vendor-wide, not per-tenant. Set
each as a Fly secret once at deploy time; tenant identity comes from
the URL path and the webhook payload itself. (Stripe / Slack / Twilio /
GitHub all run multi-tenant webhook integrations this way.)

For RingCentral and Webex you choose any high-entropy string and pass
it to the vendor when creating the subscription. For Zoom, the value
is the marketplace app's Secret Token, which Zoom issues — Zoom's
secret is already per-app, not per-tenant, by their own design.

```
flyctl secrets set --app linda-staging \
  RINGCENTRAL_WEBHOOK_SECRET=$(openssl rand -hex 32) \
  WEBEX_WEBHOOK_SECRET=$(openssl rand -hex 32) \
  ZOOM_PHONE_WEBHOOK_SECRET=<paste from marketplace.zoom.us app config>
```

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
- [ ] Build subscription-creation automation: when a customer
      completes OAuth, LINDA should call RC's Subscription API to
      create the telephony-session subscription, passing
      `RINGCENTRAL_WEBHOOK_SECRET` as the verification token. Same
      string for every customer. Not yet implemented; today the
      operator can do this via curl after each customer OAuths.
- [ ] Build subscription-renewal automation. RC subscriptions expire
      after `expiresIn` seconds (recommend 7776000 = 90 days);
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
- [ ] Build webhook-creation automation: after a customer's OAuth,
      LINDA should call `POST https://webexapis.com/v1/webhooks`
      (resource `recordings`, event `created`) passing
      `WEBEX_WEBHOOK_SECRET` as the `secret` field. Same string for
      every customer. Not yet implemented; today the operator can
      curl it manually.
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
- [ ] Copy the Zoom Marketplace **Secret Token** into the
      `ZOOM_PHONE_WEBHOOK_SECRET` Fly secret. Zoom issues this
      app-level (not per-tenant) so it's set once for the whole app.
- [ ] Submit for **marketplace review** (typically multiple weeks).
      Until approved, only accounts in your developer org can install.
- [ ] Confirm tenants connect with an account-level admin OAuth user
      (the `phone:*:admin` scopes don't work on user-level OAuth).

## Cross-cutting

- [ ] Build the post-OAuth subscription/webhook auto-creation flow
      so customer admins don't need any manual webhook setup. RC and
      Webex both expose REST endpoints to create the subscription —
      LINDA should call them right after the OAuth callback completes,
      passing the vendor-wide signing secret. Today this is a manual
      curl step the operator does once per customer.
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
