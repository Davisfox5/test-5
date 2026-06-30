# Limited Use Compliance & Gmail Data-Path Audit

Scope: LINDA's handling of Google user data obtained via the restricted
`gmail.readonly` scope (and the accompanying `gmail.send`, `calendar.events`,
`contacts.readonly` scopes), audited against Google's
[Limited Use requirements](https://developers.google.com/terms/api-services-user-data-policy#additional_requirements_for_specific_api_scopes).

This document (a) traces Gmail data end-to-end through the system, (b) maps
each Limited Use requirement to the implementation, and (c) records the gaps
found in the audit and how they were closed.

> Status: the three gaps found (A, B, C) are addressed in branch
> `feat/google-oauth-verification`. A/B are code-enforced; C is a documented
> policy with the deletion-on-disconnect mechanism now in place.

---

## 1. End-to-end Gmail data path

| Stage | Where | What happens |
|-------|-------|--------------|
| Consent & token grant | [`backend/app/api/oauth.py`](../backend/app/api/oauth.py) `oauth_authorize` / `oauth_callback` | User grants scopes; we exchange the code for access + refresh tokens and **encrypt them at rest** before storing on the `Integration` row. |
| Token storage | `Integration` model ([`backend/app/models.py`](../backend/app/models.py)) | `access_token` / `refresh_token` stored as Fernet ciphertext. |
| New-mail signal | [`backend/app/api/email_push.py`](../backend/app/api/email_push.py) | Gmail Pub/Sub push (and a polling fallback in `services/email_ingest/poller.py`) signals new messages; a Celery task is enqueued. |
| Fetch | [`backend/app/services/email_ingest/gmail.py`](../backend/app/services/email_ingest/gmail.py) | Reads messages via the Gmail API (`format=full`): headers, body, attachments. Read-only. |
| Normalize + persist | [`backend/app/services/email_ingest/ingest.py`](../backend/app/services/email_ingest/ingest.py) | Creates an `Interaction` (`channel="email"`, `source="gmail"`) with subject, body text/HTML, from/to/cc/bcc, message IDs; attachment **bytes** go to S3 (SSE-AES256), rows to `interaction_attachments`. |
| Analyze | [`backend/app/services/ai_analysis.py`](../backend/app/services/ai_analysis.py) via [`llm_client.py`](../backend/app/services/llm_client.py) | The relevant content is sent to the **Anthropic Claude API** to produce summary/sentiment/action items/coaching. |
| Surface | Dashboard / SPA | The user sees the analysis. |
| Disconnect | `oauth_revoke` ([`backend/app/api/oauth.py`](../backend/app/api/oauth.py)) | Revokes upstream, deletes tokens, **purges ingested email + attachments** (see §3). |

## 2. Limited Use requirement → implementation

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Use data **only** to provide/improve user-facing features | ✅ | Gmail content is read solely to produce the in-product analysis (`ai_analysis.py`); no other consumer of email `Interaction` content. |
| **Do not transfer** except to provide/improve features, for legal reasons, or M&A | ✅ | Only subprocessor that receives email content is Anthropic (to generate the analysis). Disclosed in the [privacy policy](https://lindaai.net/privacy). |
| **No human access** except consent / security / legal / aggregated-anonymized | ✅ (policy) | No code path exposes raw mail to staff; access restricted to the tenant's own authorized users. Stated in privacy policy §4.2. |
| **Not sold** | ✅ | No sale of data; stated in policy. |
| **Not for advertising** | ✅ | No ad systems; stated in policy. |
| **Not used to train generalized/non-personalized AI/ML models** | ✅ | See §4 — Anthropic contractually does not train on API content, and LINDA trains no models on user data. |
| Encryption in transit | ✅ | TLS everywhere (Gmail API, Anthropic API, Fly ingress). |
| Encryption at rest | ✅ | Tokens: Fernet (see §5). Attachments: S3 SSE-AES256 (`attachment_store.py`). |
| Deletion on disconnect / account deletion | ✅ (after fix) | See §3 (Gap B closure) and tenant hard-delete in `services/tenant_dataops.py`. |

## 3. Gaps found and closed

### Gap A — revoke did not revoke the grant upstream with Google *(closed)*

**Before:** `POST /oauth/{provider}/revoke` deleted only the local `Integration`
row. The access/refresh tokens remained **valid at Google** until natural
expiry.

**Fix:** `oauth_revoke` now calls Google's revocation endpoint
(`https://oauth2.googleapis.com/revoke`) for the stored refresh token (falling
back to the access token) before deleting the row. Revoking the refresh token
invalidates derived access tokens too. The call is best-effort and never blocks
the local disconnect (a 400 for an already-invalid token is treated as success).
See `_revoke_google_token` in [`backend/app/api/oauth.py`](../backend/app/api/oauth.py).

### Gap B — disconnect did not purge already-ingested mail *(closed)*

**Before:** revoke deleted the token but left every email `Interaction` (and its
attachments) we had already ingested from that mailbox in the database — i.e. a
disconnect did not delete the user's mail copy.

**Fix:** `oauth_revoke` now calls `_purge_ingested_email`, which deletes every
`Interaction` for the tenant with `channel="email"` and the provider's `source`
(`gmail` / `microsoft`), and best-effort deletes the corresponding attachment
**bytes** from S3 (`AttachmentStore.delete`). Dependent rows cascade via FK
`ON DELETE`. This makes disconnect a genuine deletion event, satisfying the
Limited Use "delete on disconnect" expectation. Covered by
[`tests/test_oauth_revoke.py`](../tests/test_oauth_revoke.py).

> Scoping note: integrations are unique per `(tenant, provider)`
> (`_upsert_integration` uses `scalar_one_or_none`), so purging all of a
> tenant's `source="gmail"` email on Google revoke is correct and complete.

### Gap C — no retention TTL on email interactions *(documented policy)*

**Finding:** unlike `webhook_deliveries` (90-day) and `feedback_events`
(365-day), email `Interaction` rows have no automatic TTL; they persist until
disconnect or tenant hard-delete.

**Disposition:** retention "for as long as the account is active" is a
defensible, disclosed policy (now stated in the privacy policy §8), and the two
deletion triggers that Limited Use actually cares about — **disconnect** (Gap B)
and **account deletion** (tenant hard-delete) — both now fully purge the data.
A scheduled TTL for email interactions can be added later if a shorter
retention commitment is desired; the mechanism (a periodic purge by age) mirrors
the existing retention sweeps. No code change required for compliance.

## 4. No model training on user data — verified

LINDA's AI analysis runs on the **Anthropic Claude API**
([`backend/app/services/llm_client.py`](../backend/app/services/llm_client.py)).

Anthropic's commercial terms state: **"Anthropic may not train models on
Customer Content from Services."**
(<https://www.anthropic.com/legal/commercial-terms>). Inputs and Outputs
submitted through the API are therefore not used for model training.

LINDA itself trains **no** models on user email content — there is no training
pipeline in the codebase; email content is used only to generate the per-user
analysis at request time. This satisfies the Limited Use prohibition on using
Google user data to develop or improve generalized/non-personalized AI/ML
models, and the privacy policy states it explicitly (§4.2, §4.3).

## 5. Encryption at rest — confirmed (with a doc nit)

OAuth tokens are encrypted before storage in
[`backend/app/services/token_crypto.py`](../backend/app/services/token_crypto.py)
using **Fernet** (AES-128-CBC + HMAC-SHA256), keyed by the
`TOKEN_ENCRYPTION_KEY` secret (a Fly secret; required in production — the code
raises `TokenCryptoNotConfigured` if unset outside DEBUG). Decryption tolerates
legacy plaintext and re-encrypts on the next refresh.

> **Doc nit (cosmetic, not a compliance issue):** the `Integration` model
> column comments in [`backend/app/models.py`](../backend/app/models.py) say
> "AES-256", but Fernet is **AES-128**-CBC with HMAC-SHA256. The encryption is
> strong and standard; only the inline comment's bit-count is inaccurate. Worth
> correcting the comment to avoid confusing a future auditor. (Attachment S3
> objects are separately encrypted with SSE-**AES256**, which is where the "256"
> is actually correct.)

## 6. Residual items (non-blocking)

- **`support@lindaai.net` / `privacy@lindaai.net`** must be live mailboxes —
  required for the consent screen and for honoring data-rights requests.
- **Microsoft upstream revoke:** Microsoft has no simple token-revocation
  endpoint equivalent to Google's; MS revocation is user/admin-driven via the
  account portal. Our local purge (Gap B) still runs for `microsoft`. This is
  acceptable for Google verification (which concerns Google data) and is noted
  for completeness.
- **Optional hardening:** add an `AuditLog` entry on each revoke (success/fail
  of upstream revocation + purge counts) for a clean compliance trail.
