# RingCentral integration

## What this gives a tenant

After connecting their RingCentral account, every recorded call (per
RingCentral's own recording policy — automatic / on-demand) becomes
an `Interaction` row in LINDA with the audio downloaded, transcribed,
and run through the standard analysis pipeline.

## Provider entry

`OAUTH_PROVIDERS["ringcentral"]` in `backend/app/api/oauth.py`:

| Key                | Value                                                                            |
| ------------------ | -------------------------------------------------------------------------------- |
| `authorize_url`    | `https://platform.ringcentral.com/restapi/oauth/authorize`                       |
| `token_url`        | `https://platform.ringcentral.com/restapi/oauth/token`                           |
| `scopes`           | `ReadAccounts`, `ReadCallLog`, `ReadCallRecording`, `Subscriptions`              |
| `client_id_key`    | `RINGCENTRAL_CLIENT_ID` env var                                                  |
| `client_secret_key`| `RINGCENTRAL_CLIENT_SECRET` env var                                              |
| `certified`        | `False` (until the developer-portal app is graduated to Production)              |

Tenants on the RC sandbox can override the host via
`Integration.provider_config["api_base"] = "https://platform.devtest.ringcentral.com"`.

## Setup steps the operator runs

1. **Create a RingCentral developer app** at developer.ringcentral.com.
   - **Auth type**: Server (3-legged OAuth — Authorization Code).
   - **Permissions**: must include the four scopes above (RC labels
     them as "App Permissions").
   - **Redirect URI**: `https://<linda-host>/api/v1/oauth/ringcentral/callback`.
2. **Set env vars** on the LINDA backend:
   - `RINGCENTRAL_CLIENT_ID`
   - `RINGCENTRAL_CLIENT_SECRET`
3. **Graduate the app to Production** (RC sandbox apps can only be used
   by accounts in your developer org).
4. **For each customer tenant connecting their RC account**: tenant
   admin runs the OAuth connect flow from the LINDA settings page; on
   success an `Integration` row with `provider="ringcentral"` is
   created with encrypted access + refresh tokens.
5. **Subscribe to telephony-session events**. After OAuth, POST to
   `https://platform.ringcentral.com/restapi/v1.0/subscription` with:

```json
{
  "eventFilters": [
    "/restapi/v1.0/account/~/extension/~/telephony/sessions"
  ],
  "deliveryMode": {
    "transportType": "WebHook",
    "address": "https://<linda-host>/api/v1/uc/ringcentral/webhook/<tenant-id>",
    "verificationToken": "<random-32-byte-string>"
  },
  "expiresIn": 7776000
}
```

6. **Persist the verification token** on the tenant's Integration row:
   ```
   POST /api/v1/admin/integrations/uc/ringcentral/webhook-secret
   { "webhook_secret": "<the-same-verification-token>" }
   ```

## Webhook flow

1. RC sends a one-time `Validation-Token` header to the new subscription
   URL with no body. LINDA echoes it back as a response header on a
   200 — see `RingCentralProvider.verify_webhook` for the steady-state
   verification token check.
2. Steady-state: every telephony-session event with a recorded leg
   carries the `Verification-Token` header. The route handler checks
   it against `Integration.provider_config["webhook_secret"]`.
3. Authenticated payload becomes a `UcRecordingJob` row keyed on
   `(provider="ringcentral", external_call_id=<telephonySessionId>)`.
4. Celery task `fetch_uc_recording` downloads the recording from the
   `contentUri` in the payload (Bearer-auth'd with the OAuth access
   token) and dispatches the standard voice pipeline.

## Subscription renewal

RingCentral subscriptions expire after `expiresIn` seconds (default
90 days here). A future scheduled task should re-up via PUT on
`/restapi/v1.0/subscription/{id}` before each expiry. Not yet
implemented — tracked in `USER_TODO.md`.
