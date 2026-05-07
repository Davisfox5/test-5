# Zoom Phone integration

## What this gives a tenant

After connecting Zoom Phone, every Zoom Phone call recording becomes
an `Interaction` row in LINDA with the recording downloaded from
Zoom's `download_url`.

## Provider entry

`OAUTH_PROVIDERS["zoom_phone"]` in `backend/app/api/oauth.py`:

| Key                | Value                                                                                  |
| ------------------ | -------------------------------------------------------------------------------------- |
| `authorize_url`    | `https://zoom.us/oauth/authorize`                                                      |
| `token_url`        | `https://zoom.us/oauth/token`                                                          |
| `scopes`           | `phone:read:admin`, `phone_recording:read:admin`, `phone_call_log:read:admin`          |
| `client_id_key`    | `ZOOM_PHONE_CLIENT_ID` env var                                                         |
| `client_secret_key`| `ZOOM_PHONE_CLIENT_SECRET` env var                                                     |
| `certified`        | `False` (until the marketplace listing is approved)                                    |

This is a **separate Zoom marketplace app** from the existing `zoom`
entry (which is for meetings). Reusing the meetings app's
`zoom:meeting:*` scopes does NOT grant Phone API access.

## Setup steps the operator runs

1. **Create a Zoom marketplace OAuth app** at marketplace.zoom.us.
   - **App type**: OAuth (server-to-server is also possible but uses
     a different scope model).
   - **Scopes (admin-level)**: enable the three above.
   - **Redirect URI**: `https://<linda-host>/api/v1/oauth/zoom_phone/callback`.
   - **Event Subscriptions**: enable `phone.recording_completed`.
   - **Endpoint URL** (event subscription):
     `https://<linda-host>/api/v1/uc/zoom/webhook/<tenant-id>`.
2. **Set env vars** on the LINDA backend:
   - `ZOOM_PHONE_CLIENT_ID`
   - `ZOOM_PHONE_CLIENT_SECRET`
3. **Submit for marketplace review** (typically multiple weeks; needed
   for non-developer-org customers to install).
4. **For each customer tenant**: admin runs OAuth connect from LINDA
   settings; an `Integration` row with `provider="zoom_phone"` is
   created.
5. **Persist the marketplace app's Secret Token** on the tenant's
   Integration row:
   ```
   POST /api/v1/admin/integrations/uc/zoom_phone/webhook-secret
   { "webhook_secret": "<marketplace-secret-token>" }
   ```

## Webhook flow

1. **URL-validation handshake** (one-time, when the endpoint URL is
   first set on the marketplace app): Zoom sends
   `event = "endpoint.url_validation"` with `payload.plainToken`. LINDA
   replies 200 with
   `{"plainToken": ..., "encryptedToken": <hmac sha256>}`. Handled in
   `uc_telephony.zoom_phone_webhook` before any signature check.
2. **Steady-state**: Zoom signs every webhook with HMAC-SHA256 over
   `v0:{x-zm-request-timestamp}:{body}` and emits `v0={hex}` in
   `x-zm-signature`. LINDA verifies via
   `ZoomPhoneProvider.verify_webhook`.
3. Authenticated payload becomes a `UcRecordingJob` row keyed on
   `(provider="zoom_phone", external_call_id=<call_id>)`.
4. Celery task `fetch_uc_recording` downloads the recording from
   `payload.object.recording_files[0].download_url` (Bearer-auth'd
   with the OAuth access token) and dispatches the standard voice
   pipeline.

## Notes

- Zoom Phone download URLs require an `Authorization: Bearer` header.
  The OAuth access token must come from an account-level admin (one of
  the `phone:*:admin` scopes). User-level OAuth tokens cannot fetch
  recordings made by other users on the same account.
- Recording file types are MP3 (default) or M4A. Both flow through the
  normalizer's `pydub` path.
