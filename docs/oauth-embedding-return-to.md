# Embedding LINDA's OAuth — post-connect `return_to`

An external app (e.g. a super-admin console) can trigger a LINDA mailbox
connect on behalf of a tenant and have the user returned to **its own** UI
afterward, instead of being bounced into LINDA's SPA.

## How it works

Both connect entry points accept an optional `return_to`:

- `POST /api/v1/oauth/{provider}/ticket?return_to=<url>` (authenticated;
  api-key or session) — mints the authorize URL the caller redirects to.
- `GET /api/v1/oauth/{provider}/authorize?return_to=<url>` (api-key/tooling).

`return_to` is stored in the OAuth state (Redis) alongside the tenant context.
After a successful connect, the callback redirects to
`<return_to>?integration_connected=<provider>` instead of LINDA's SPA.

## Landing behavior (callback `_finish_connect`)

Precedence after a successful connect:

1. **Valid `return_to`** → redirect there with `?integration_connected=<provider>`.
2. **No `return_to`, but api-key-initiated** → plain `200 {"status":"connected"}`.
   An api-key caller isn't a LINDA SPA user, so it is **not** redirected into
   the SPA. (`source=="api_key"` is captured from the principal at ticket time.)
3. **Otherwise** → the provider's existing default (Google/Microsoft → LINDA
   SPA `/settings`; CRMs → `{"status":"connected"}`).

So an external console gets the clean embedded experience either by passing
`return_to` (preferred — user lands back in the console) or, if it omits it,
by not being thrown into LINDA's SPA.

## Allowlisting (open-redirect guard)

`return_to` must be an absolute `http(s)` URL whose origin is allowlisted;
anything else is ignored (falls through to the default). Allowed origins:

- every entry in `ALLOWED_ORIGINS`,
- the `SPA_URL` origin,
- every entry in the new **`OAUTH_RETURN_TO_ALLOWED_ORIGINS`** (for consoles
  that don't otherwise need CORS).

Plain `http` is rejected except `localhost`/`127.0.0.1` when `DEBUG` is on.

Configure the console's origin as a Fly secret on `linda-staging`, e.g.:

```
OAUTH_RETURN_TO_ALLOWED_ORIGINS=["https://console.example.com"]
```

## Example

```
POST https://lindaai.net/api/v1/oauth/google/ticket
     ?return_to=https://console.example.com/customers/42/integrations
Authorization: Bearer <tenant api key>
→ { "authorize_url": "https://accounts.google.com/o/oauth2/auth?..." }
```

Console redirects the user to `authorize_url`; after consent the user lands at
`https://console.example.com/customers/42/integrations?integration_connected=google`.

## Note on signup auto-provisioning (related)

`POST /trial/signup` mints a sandbox tenant on first SPA login with **no**
allowlist/invite gating, so a stray test login leaves a real tenant behind.
That's not changed here; `scripts/cleanup_stray_tenant.py` removes such a
tenant safely (dry-run by default).
