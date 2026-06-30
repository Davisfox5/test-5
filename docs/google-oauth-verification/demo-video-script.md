# Demo Video Script

Google requires a video that shows the **OAuth consent flow** and, for each
requested scope, **how the granted data is used in the product**. Record at
1080p, screen + cursor visible, narrated or captioned. Keep it ~3–5 minutes.
Use a real test mailbox (e.g. `davisfox5@gmail.com`) with a few representative
emails. An unlisted YouTube link is accepted.

> Record against the production domain `https://lindaai.net` so the consent
> screen and redirect URI shown on camera match what's submitted. The OAuth URL
> shown should clearly display the **app name "Linda AI"** and the **scopes**
> being requested.

---

## Shot 1 — Identify the app (10s)

- Show `https://lindaai.net/` (the homepage) so the reviewer sees the real,
  branded site whose domain is on the consent screen.
- Narrate: *"This is Linda AI, a call- and email-intelligence tool for sales
  and support teams. I'll connect a Gmail mailbox and show how each requested
  permission is used."*

## Shot 2 — Start the connect flow (15s)

- In the app, go to **Settings → Integrations** (or the "Connect mailbox"
  entry point) and click **Connect Google**.
- This calls `GET /api/v1/oauth/google/authorize`
  ([`backend/app/api/oauth.py`](../../backend/app/api/oauth.py)), redirecting to
  Google.

## Shot 3 — The consent screen + grant (25s) — **required**

- Show the Google consent screen in full: the **"Linda AI"** app name and the
  **list of requested scopes** (Gmail read, Gmail send, Calendar events,
  Contacts). Pause so each is readable.
- Choose the test account, click through, and **grant** access.
- Show the redirect back to `https://lindaai.net/...` completing the connect
  (the callback at `/api/v1/oauth/google/callback`). Show the UI confirming the
  mailbox is connected.

## Shot 4 — `gmail.readonly` in use (60s) — **the restricted scope**

- Show LINDA ingesting and analyzing an email conversation from the connected
  mailbox. Open an analyzed email interaction in the dashboard.
- Point to the AI-generated output derived from the message body: **summary,
  sentiment, action items, coaching insights.**
- Narrate that this is exactly why read access is needed — *"the insights come
  from the content of the email, so Linda reads the message body and
  attachments to produce them. Linda never modifies or deletes the mail."*
- (Optional) Show an analyzed email with an attachment to demonstrate why
  attachment access matters.

## Shot 5 — `gmail.send` in use (30s)

- From a conversation, open a **suggested follow-up email**. Edit/approve it,
  then click **Send**.
- Show the sent message landing in the test mailbox's Sent folder.
- Narrate: *"Linda only sends a message the user has reviewed and approved —
  never automatically."*

## Shot 6 — `calendar.events` in use (25s)

- From a follow-up, **schedule an event** (e.g. "book next call"). Show LINDA
  creating the event.
- Switch to Google Calendar and show the created event.

## Shot 7 — `contacts.readonly` in use (20s)

- Show a conversation correctly **attributed to a customer/contact**, and
  explain that contacts read access is used to resolve the people in the
  conversation to the right contact record.

## Shot 8 — Disconnect & deletion (25s) — strongly recommended

- Go back to **Integrations** and click **Disconnect** (calls
  `POST /api/v1/oauth/google/revoke`).
- Narrate the Limited Use deletion guarantee: *"Disconnecting revokes Linda's
  access at Google, deletes the stored tokens, and purges the email data Linda
  ingested from this mailbox."* (This maps to the revoke handler in
  [`backend/app/api/oauth.py`](../../backend/app/api/oauth.py); see
  [`../limited-use-compliance.md`](../limited-use-compliance.md).)
- Optionally show the Google Account → Security → Third-party access page no
  longer listing Linda after revoke.

---

## Checklist before uploading

- [ ] Consent screen shows **app name "Linda AI"** and **all four scopes**, readable.
- [ ] Redirect lands on `lindaai.net` (matches a submitted redirect URI).
- [ ] Each scope has a visible in-product use on camera.
- [ ] No secrets/tokens visible on screen (don't show `.env`, the client
      secret, or raw access tokens).
- [ ] Video is unlisted/public so the reviewer can open it.
