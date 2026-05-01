# Notification surface audit

Snapshot of what the LINDA platform actually does today when something
"notifies" a user. Scope: explicit notification mechanisms that surface
events to humans. Out of scope: webhook fan-out (covered by
`webhook_dispatcher`) and inbound channel push subscriptions
(`/email-push`, telephony webhooks).

## What's wired

### Slack + Microsoft Teams (outbound webhooks)

`backend/app/services/notification_service.py` implements
`NotificationService.notify_slack(webhook_url, message, blocks?)` and
`notify_teams(webhook_url, title, text)`. Both POST best-effort; failures
are logged, never retried.

The only live consumer is the weekly **vocabulary digest**:
`backend/app/services/digest_service.py::send_vocabulary_digests` walks
every tenant whose `features_enabled["slack_vocab_digest_webhook"]` is
set and posts a Slack message listing pending vocabulary candidates.
There is no Teams call site — the function exists but nothing invokes
it.

### Webhook subscriptions (tenant-configured)

`backend/app/services/webhook_dispatcher.py` + `Webhook` /
`WebhookDelivery` models give tenants HMAC-signed outbound HTTP
deliveries. The retention sweep
(`backend/app/services/event_retention.py`) keeps deliveries for 90d.
This is platform-grade and shipped — covered separately from the
"notification" surface here, but worth flagging because it's the
mechanism users would reach for to bridge to PagerDuty / Discord /
Notion etc.

### Trial expiry "notices"

`backend/app/tasks.py::trial_expiry_daily` walks sandbox tenants and
emits a per-bucket notice (3d / 1d / expired) — but the "emit" is a
`logger.info` line plus a `TenantDataOpsLog` audit row. There is no
email / Slack / push delivery. Comments in the code explicitly call out
the missing transport: *"if/when an email provider is wired the same
code can switch to that transport."*

### Vector-health alerts (developer-facing)

`backend/app/services/kb/vector_health_check.py` files a GitHub issue
on the configured repo when the pgvector p95 latency streak crosses
threshold. Single recipient (the dev team), keyed on `GITHUB_TOKEN` /
`GITHUB_REPO` env vars. Not a tenant-facing notification path.

## What's stubbed

- **Email transport.** No SMTP / SES / SendGrid / Postmark client is
  wired. The `backend/app/services/email/` package handles *outbound
  channel emails* (a tenant's reply to a customer via Gmail / Graph) —
  not transactional / system email. Trial-expiry, password reset,
  webhook-failure summaries all have no email path.
- **Microsoft Teams notifier.** `notify_teams` exists but no caller.
- **In-app notification feed.** The SPA has no bell icon / notification
  list / unread counter. `apps/app/src/app/(app)/` has no
  `notifications/` route. Action items live at `/action-items` but they
  are work items, not event notifications.

## What's missing

- **Tenant-controllable notification preferences.** No UI for "email me
  on dead-letter webhook," "Slack me on at-risk renewal." The only
  toggle is the Slack vocab-digest webhook hidden inside
  `features_enabled`.
- **Notification model + persistence.** No `notifications` table, no
  unread-state, no read-receipt. Every notification today is fire-and-
  forget logging.
- **Fan-out routing.** No abstraction to take an event and decide
  per-tenant per-user-role which transports fire — every call site
  hard-codes Slack via the digest service.
- **Critical-path coverage.** Trial expiring, webhook subscription dead-
  lettered, scorecard regression detected, churn-risk crossing
  threshold — none of these reach a human today outside of dashboard
  reload.

## Bottom line

Two-and-a-half live notification paths (Slack vocab digest, GitHub
issues, half a Teams notifier) and no email / in-app surface.
Tenant-facing notifications are effectively the dashboard plus
outbound webhooks customers wire themselves.
