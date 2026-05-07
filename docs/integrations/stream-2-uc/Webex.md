# Webex Calling integration

## What this gives a tenant

After connecting Webex, every recorded Webex Calling session becomes
an `Interaction` row in LINDA with audio downloaded via the Converged
Recordings API.

## Provider entry

`OAUTH_PROVIDERS["webex_calling"]` in `backend/app/api/oauth.py`:

| Key                | Value                                                                                                                                      |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `authorize_url`    | `https://webexapis.com/v1/authorize`                                                                                                       |
| `token_url`        | `https://webexapis.com/v1/access_token`                                                                                                    |
| `scopes`           | `spark:calls_read`, `spark:recordings_read`, `spark-admin:recordings_read`, `spark-admin:telephony_config_read`                            |
| `client_id_key`    | `WEBEX_CLIENT_ID` env var                                                                                                                  |
| `client_secret_key`| `WEBEX_CLIENT_SECRET` env var                                                                                                              |
| `use_pkce`         | `True` — Webex requires PKCE (code_verifier + code_challenge S256). Handled automatically by the oauth.py authorize URL builder.           |
| `certified`        | `False` (until the developer-portal integration is graduated)                                                                              |

## Setup steps the operator runs

1. **Create a Webex Developer integration** at developer.webex.com.
   - **OAuth scopes**: enable the four scopes above. The
     `spark-admin:*` scopes require an admin-level Webex account
     during connect.
   - **Redirect URI**: `https://<linda-host>/api/v1/oauth/webex_calling/callback`.
   - PKCE is enabled by default for new integrations — confirm S256.
2. **Set env vars** on the LINDA backend:
   - `WEBEX_CLIENT_ID`
   - `WEBEX_CLIENT_SECRET`
3. **App Hub listing** (for customer-installable distribution) — submit
   at developer.webex.com → My Apps → App Hub. Required for
   non-developer-org customers to install.
4. **For each customer tenant**: admin runs OAuth connect from LINDA
   settings; an `Integration` row with `provider="webex_calling"` is
   created.
5. **Subscribe to recording events** via the Webhooks API. After the
   admin OAuth completes, POST to `https://webexapis.com/v1/webhooks`
   with:

```json
{
  "name": "LINDA recordings",
  "targetUrl": "https://<linda-host>/api/v1/uc/webex/webhook/<tenant-id>",
  "resource": "recordings",
  "event": "created",
  "secret": "<random-32-byte-string>"
}
```

6. **Persist the webhook secret** on the tenant's Integration row:
   ```
   POST /api/v1/admin/integrations/uc/webex_calling/webhook-secret
   { "webhook_secret": "<the-same-secret>" }
   ```

## Webhook flow

1. Webex signs every delivery with HMAC-SHA1 over the request body in
   the `X-Spark-Signature` header. LINDA verifies via
   `WebexProvider.verify_webhook`.
2. The webhook envelope only carries `data.id` (the recording id);
   LINDA fetches metadata via
   `GET https://webexapis.com/v1/recordings/{id}` (Bearer-auth'd) and
   downloads the audio from the returned
   `temporaryDirectDownloadLinks.audioDownloadLink` (signed URL — no
   auth header on that fetch).
3. Authenticated payload becomes a `UcRecordingJob` row keyed on
   `(provider="webex_calling", external_call_id=<recording-id>)`.

## Notes

- Webex's Converged Recordings API is region-aware. The default base
  URL `webexapis.com` works for most regions; tenants in segregated
  data centers can override via
  `Integration.provider_config["api_base"]`.
- Recording format is typically MP4 audio; the normalizer handles MP3
  / WAV / FLAC out of the box. MP4 audio works through pydub via
  ffmpeg — verify ffmpeg is on PATH on the worker host.
