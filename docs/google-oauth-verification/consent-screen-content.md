# OAuth Consent Screen — Exact Content & Console Steps

Everything here is entered in the **Google Cloud Console** (browser, manual).
Project `107620016884`. This file is the copy-paste source.

## App registration / OAuth consent screen

| Field | Value |
|-------|-------|
| App name | `Linda AI` |
| User support email | `support@lindaai.net` |
| App logo | See **Logo notes** below |
| Application home page | `https://lindaai.net/` |
| Application privacy policy link | `https://lindaai.net/privacy` |
| Application terms of service link | `https://lindaai.net/terms` |
| Authorized domain | `lindaai.net` |
| Developer contact email | (the project owner's monitored address) |
| User type | External |
| Publishing status | Testing → **In production** (set when submitting for verification) |

## OAuth client (Web application)

| Field | Value |
|-------|-------|
| Client ID | `107620016884-g3k4i74ole3jap4k6d8dkmfbcurliu04.apps.googleusercontent.com` |
| Authorized redirect URIs | `https://lindaai.net/api/v1/oauth/google/callback` |
| | `https://linda-staging.fly.dev/api/v1/oauth/google/callback` |

> The `lindaai.net` redirect URI is the one customers will use in production.
> The `linda-staging.fly.dev` URI is kept for internal/staging connects. Both
> resolve to the same app and the same handler
> ([`oauth_callback` in `backend/app/api/oauth.py`](../../backend/app/api/oauth.py)).

## Scopes to add

Add these four under "Data access" / scopes (justifications in
[`scope-justifications.md`](scope-justifications.md)):

```
https://www.googleapis.com/auth/gmail.readonly      (restricted)
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/calendar.events
https://www.googleapis.com/auth/contacts.readonly
```

## Logo notes

- **Format/size:** PNG or JPG, square, **120×120 px** minimum (Google
  recommends an exact square; 120×120 is the documented minimum). Max 1 MB.
- **Content:** must be LINDA's own mark and must match the branding on
  `lindaai.net`. The site uses a wordmark "LINDA" with an indigo→violet
  gradient (`#6366F1` → `#8B5CF6`) on a dark background — produce a square
  logo consistent with that (e.g. the wordmark or a monogram on a dark or
  transparent tile).
- **Constraint:** once a logo is set on a consent screen that's in
  verification, changing it can re-trigger brand review. Finalize the logo
  before submitting.
- The repo currently uses a 📞 emoji favicon as a placeholder — that is **not**
  acceptable as the consent-screen logo; a real logo asset is required.

## Manual Console step list (in order)

1. **Search Console:** verify `lindaai.net` ownership under the project owner's
   account (see [`README.md`](README.md) → Deliverable 7).
2. **OAuth consent screen → Branding:** set app name, support email, logo,
   home page, privacy, terms, authorized domain (table above).
3. **OAuth consent screen → Data access:** add the four scopes; paste each
   justification.
4. **Credentials → OAuth client:** confirm both redirect URIs are present.
5. **Audience:** confirm User type = External. (Test users no longer matter
   once verified/in production, but leaving the current test users is harmless.)
6. **Demo video:** record per [`demo-video-script.md`](demo-video-script.md),
   upload (unlisted YouTube accepted), and paste the link into the submission.
7. **Submit for verification.** For the restricted `gmail.readonly` scope this
   triggers the CASA security-assessment track — see
   [`../casa-readiness.md`](../casa-readiness.md).

## Pre-submit sanity checks (can be run from anywhere)

```bash
# All three must return HTTP 200 from the public domain after deploy:
curl -sS -o /dev/null -w "%{http_code}\n" https://lindaai.net/
curl -sS -o /dev/null -w "%{http_code}\n" https://lindaai.net/privacy
curl -sS -o /dev/null -w "%{http_code}\n" https://lindaai.net/terms

# The privacy page must contain the Limited Use disclosure:
curl -sS https://lindaai.net/privacy | grep -c "API Services User Data Policy"
```
