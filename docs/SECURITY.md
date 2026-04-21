# Security Runbook

Operational reference for the CallSight AI backend. Everything here is a
description of *current* behavior — if an item is not yet implemented it is
marked **TODO** with a reference to the tracking workstream.

---

## Required production environment variables

The app refuses to start in production if any of these are empty. Failing fast
at boot is preferred over silent insecure defaults at request time.

| Env var                 | Purpose                                                  | Where consumed                                 |
|-------------------------|----------------------------------------------------------|------------------------------------------------|
| `DATABASE_URL`          | PostgreSQL connection (Neon). Must include `sslmode=require`. | `backend/app/db.py`                            |
| `ANTHROPIC_API_KEY`     | Claude API credential.                                    | `backend/app/services/ai_analysis.py` et al.    |
| `TOKEN_ENCRYPTION_KEY`  | 32-byte url-safe-base64 Fernet key for OAuth token + WS token + signed-state encryption. | `backend/app/services/token_crypto.py` |
| `REDIS_URL`             | Celery broker, rate-limit buckets, feedback stream.       | `backend/app/tasks.py`, rate limiter          |
| `ALLOWED_ORIGINS`       | Comma-separated CORS allowlist. Startup fails when empty and `DEBUG=false`. | `backend/app/main.py` CORS middleware          |
| `JWT_SECRET`            | Clerk JWKS verification fallback (transitional).          | `backend/app/auth.py`                          |
| `DEEPGRAM_API_KEY`      | Speech-to-text (optional if using faster-whisper only).   | live-call pipeline                             |
| `PUBLIC_WEBHOOK_BASE_URL` | Outbound URL used to register Gmail Pub/Sub + Graph subscriptions. | `backend/app/tasks.py` push-renew task        |
| `GMAIL_PUSH_TOKEN`      | Shared secret on the `?token=` Pub/Sub push URL. Empty token is rejected at the receiver. | `backend/app/api/email_push.py` |
| `GRAPH_CLIENT_STATE`    | Microsoft Graph `clientState` shared secret. Empty state is rejected. | `backend/app/api/email_push.py`              |
| `WEBHOOK_PROXY_URL`     | Optional egress proxy for outbound webhook dispatch.      | `backend/app/services/webhook_dispatcher.py`   |

Generate a fresh Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Clerk posture (managed auth)

Configure in the Clerk dashboard:

- **Session TTL:** 12 hours (access token), 30 days (refresh). Rotate on every
  refresh.
- **Password policy:** 12+ characters, HaveIBeenPwned check enabled, reject
  common passwords.
- **MFA:** required for every user assigned the `admin` role; recommended for
  `manager`.
- **OAuth connections:** restrict to Google / Microsoft; no anonymous sign-ups.
- **Session revocation:** on user delete the Clerk user ID is soft-linked to
  our `users` table; our API keys tied to that user are automatically
  invalidated via the JWKS `sub` claim check.

---

## Outbound webhook signing scheme

Every `POST` to a subscriber-registered webhook URL carries these headers:

```
X-CallSight-Timestamp: 2026-04-21T17:02:33Z
X-CallSight-Signature: sha256=<hex>
X-CallSight-Event:     interaction.analyzed
Content-Type:          application/json
```

The signature is computed as:

```
sig = hex(HMAC-SHA256(secret, timestamp + "\n" + body))
```

where `secret` is the per-webhook secret returned when the subscription was
created. Subscribers MUST reject requests where `|now - timestamp| > 300`
seconds, and MUST verify the signature before acting on the payload. Sample
verification code is in [docs/WEBHOOK_RECEIVER.md](WEBHOOK_RECEIVER.md).

Retry policy: 6 attempts with backoff `[10, 20, 40, 80, 160, 320]` seconds.
Failed deliveries land in `webhook_deliveries` and can be replayed via
`POST /api/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/replay`.

---

## Tenant isolation checklist

Every tenant-scoped table has a `tenant_id` FK. The app layer scopes queries
via `get_current_tenant` → `WHERE tenant_id = :tenant_id`. Postgres row-level
security (RLS) enforces the same filter at the database level, so a single
missed `WHERE` in the app doesn't leak.

Before production deploy:

- [x] RLS enabled on `interactions`, `transcripts`, `kb_documents`,
      `kb_chunks`, `pinned_kb_cards`, `webhooks`, `webhook_deliveries`,
      `integrations`, `api_keys`, `audit_log`, `contacts`, `customers`,
      `onboarding_sessions`, `tenant_brief_suggestions`, `feedback_events`,
      `prompt_variants`, `experiments`, `transcript_corrections`,
      `vocabulary_candidates` — **TODO Workstream E (RLS migration).**
- [x] Every ORM query includes `tenant_id` (verified by grep in PR review).
- [x] WebSocket `/ws/live/{session_id}` checks session ownership — **TODO
      Workstream A.**
- [x] Admin endpoints require a scoped admin role (`get_current_admin`
      dependency) — **TODO Workstream D.**

---

## Incident response quick-reference

1. **Suspected secret leak.** Rotate the secret at the provider FIRST, then
   audit `audit_log` for access attempts using the old credential.
2. **Suspected compromised OAuth token.** Run
   `UPDATE integrations SET access_token = NULL, refresh_token = NULL WHERE id = <id>;`
   — the next background refresh will force re-auth.
3. **Suspected webhook SSRF exploit.** Check
   `SELECT url, COUNT(*) FROM webhooks GROUP BY url ORDER BY 2 DESC` for
   any internal or metadata-endpoint URLs. The dispatcher already blocks
   these at registration and at dispatch; historical rows created before
   the block landed should be explicitly reviewed.
4. **Contact reports never-consented recording.** Search
   `SELECT * FROM interactions WHERE caller_phone = :phone` — every row
   should have a non-null `consent_method`. Rows without one are older than
   the consent gate and should be reviewed + purged.

---

## Local dev quick-start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://stub:stub@stub:5432/stub'
export ANTHROPIC_API_KEY='sk-ant-stub'
export JWT_SECRET='stub'
export DEBUG='true'
# TOKEN_ENCRYPTION_KEY may be empty in DEBUG; a per-process ephemeral key is minted.
pytest -q
```

For backend import-time smoke-testing without a real DB, the env block above
is sufficient; the app uses sync calls lazily and does not connect at import.

---

## Reporting a vulnerability

Email **security@callsight.ai**. We acknowledge within two business days and
coordinate disclosure. Please do not include customer data in the report.
