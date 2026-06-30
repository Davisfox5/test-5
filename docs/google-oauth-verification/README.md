# Google OAuth Verification — Submission Pack

This directory is the working pack for submitting LINDA's Google OAuth app
for **verification of the restricted `gmail.readonly` scope**, so any customer
can connect their own Gmail/Outlook with no per-user test-user step and no
"unverified app" warning.

Restricted-scope verification is the heavy path: it requires a published
public site (homepage + privacy + terms on the consent screen's authorized
domain), a per-scope justification, a demo video, a Limited Use writeup, and a
**CASA security assessment**. This pack covers everything that lives in the
repo; the Google Cloud Console actions and the CASA engagement are external
steps and are called out as such.

## Contents

| File | What it is |
|------|------------|
| [`consent-screen-content.md`](consent-screen-content.md) | Exact values to paste into the OAuth consent screen + app registration, logo notes, and the manual Console step list. |
| [`scope-justifications.md`](scope-justifications.md) | Per-scope justification, including why `gmail.readonly` and not a narrower scope. |
| [`demo-video-script.md`](demo-video-script.md) | Shot-by-shot script for the required demo video: consent screen → grant → each scope's data used in-product. |
| [`../limited-use-compliance.md`](../limited-use-compliance.md) | Limited Use compliance writeup + end-to-end Gmail data-path audit + gap closure. |
| [`../casa-readiness.md`](../casa-readiness.md) | CASA Tier-2 readiness checklist mapping LINDA's deployment to the assessment. |

## Key facts (single source of truth)

| Field | Value |
|-------|-------|
| App name (consent screen) | **Linda AI** |
| OAuth client | One shared Web client |
| Client ID | `107620016884-g3k4i74ole3jap4k6d8dkmfbcurliu04.apps.googleusercontent.com` |
| Google Cloud project number | `107620016884` |
| User type | External |
| Requested scopes | `gmail.readonly` (restricted), `gmail.send`, `calendar.events`, `contacts.readonly` — defined in [`backend/app/api/oauth.py`](../../backend/app/api/oauth.py) (`GOOGLE_SCOPES`) |
| Authorized domain | `lindaai.net` |
| Homepage URL | `https://lindaai.net/` |
| Privacy policy URL | `https://lindaai.net/privacy` |
| Terms of service URL | `https://lindaai.net/terms` |
| Authorized redirect URIs | `https://lindaai.net/api/v1/oauth/google/callback` and `https://linda-staging.fly.dev/api/v1/oauth/google/callback` |
| Support email | `support@lindaai.net` *(must be a live, monitored mailbox — see below)* |

## Deliverable 1 — Production target being verified

**Decision: `linda-staging` is the de-facto production backend, and we verify
the `lindaai.net` domain in front of it. We are not standing up a separate
prod Fly app for this.**

Rationale:

- `lindaai.net`'s apex `A`/`AAAA` records already point at the `linda-staging`
  Fly app (dedicated IPs `37.16.11.73` / `2a09:8280:1::10b:1bf6:0`), and a
  Let's Encrypt cert is live via the DNS-01 `_acme-challenge` CNAME. So
  `https://lindaai.net/api/v1/...` already serves the API and the OAuth
  callback.
- The Gmail OAuth callback is registered on this app, and this is the
  deployment that touches Gmail data — which is exactly what **CASA scans**
  (`lindaai.net` / `linda-staging.fly.dev`).
- The Fly app being internally named `linda-staging` is **never user-visible**:
  the consent screen, redirect URIs, homepage, privacy, and terms all use the
  `lindaai.net` domain. Reviewers see `lindaai.net`, not the Fly app name.

If a dedicated `linda-prod` app is ever stood up later, the only verification
artifacts that must change are the authorized domain/redirect URIs in the
Console and the DNS records — the code and these docs are app-name agnostic.

## Deliverable 7 — Search Console domain verification (manual)

> ⚠️ **Manual Console step, must be done by the project owner.**

Before (or during) submission, `lindaai.net` must be verified as a property in
**Google Search Console** under the same Google account that owns Cloud project
`107620016884`. Google ties the consent screen's authorized domain to a
Search-Console-verified owner.

Steps:

1. Go to <https://search.google.com/search-console> signed in as the owner of
   project `107620016884`.
2. Add a **Domain** property for `lindaai.net` (preferred — covers all
   subdomains) or a **URL prefix** property for `https://lindaai.net/`.
3. For a Domain property, add the **TXT record** Google provides to Squarespace
   DNS (registrar-side; no CLI). For a URL-prefix property you can instead use
   the HTML-file or meta-tag method, but the DNS TXT method is cleanest and does
   not require deploying a verification file.
4. Confirm verification, then return to the OAuth consent screen and ensure the
   authorized domain `lindaai.net` resolves without error.

## Pre-submission checklist

- [ ] `support@lindaai.net` and `privacy@lindaai.net` are live, monitored
      mailboxes (Squarespace email or a forward). The consent screen requires a
      working support email.
- [ ] This branch is merged and `linda-staging` redeployed, so
      `https://lindaai.net/`, `/privacy`, and `/terms` all return 200 publicly.
- [ ] `lindaai.net` verified in Google Search Console (above).
- [ ] Consent screen fields set per [`consent-screen-content.md`](consent-screen-content.md).
- [ ] Logo uploaded (see logo notes in the same file).
- [ ] Demo video recorded per [`demo-video-script.md`](demo-video-script.md) and
      uploaded (unlisted YouTube is accepted).
- [ ] Scope justifications pasted per [`scope-justifications.md`](scope-justifications.md).
- [ ] Limited Use gaps closed (see [`../limited-use-compliance.md`](../limited-use-compliance.md)) — done in this branch.
- [ ] CASA engagement understood and scoped (see [`../casa-readiness.md`](../casa-readiness.md)).
