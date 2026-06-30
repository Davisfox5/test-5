# Per-Scope Justifications

Paste each justification into the matching "Scope justification" box on the
OAuth consent screen. Each one states **what the scope reads, what feature it
powers, and why a narrower scope would not work.** Keep the phrasing concrete
and tied to a visible feature — reviewers reject vague or speculative
justifications.

The scopes are defined in
[`backend/app/api/oauth.py`](../../backend/app/api/oauth.py) (`GOOGLE_SCOPES`).

---

## 1. `https://www.googleapis.com/auth/gmail.readonly` — **RESTRICTED**

> LINDA analyzes the user's email conversations to generate summaries,
> sentiment, action items, and coaching insights, alongside the same analysis
> we run on their phone calls. To do this we read the full content of messages
> in the connected mailbox — sender and recipients, subject, body, and
> attachments — and turn each conversation into an analyzed record the user
> sees in the LINDA dashboard. The analysis requires the message body and
> attachments, not just headers or metadata, because the insights (summary,
> sentiment, next steps, coaching) are derived from what was actually said. We
> never modify or delete the user's mail; access is strictly read-only.

**Why not a narrower scope:**

- `gmail.metadata` exposes only headers (From/To/Subject/labels) and **no
  message body**. LINDA's entire value is analyzing the *content* of the
  conversation — a metadata-only scope cannot produce a summary, sentiment, or
  coaching insight.
- `gmail.addons.current.message.readonly` is scoped to Gmail Add-ons running in
  the Gmail UI; LINDA is a standalone web app with a background ingestion
  pipeline (Gmail Pub/Sub push + a polling fallback), not a Gmail Add-on.
- There is no read scope narrower than `gmail.readonly` that returns message
  bodies for arbitrary messages in the mailbox. `gmail.readonly` is the
  minimum scope that supports the feature.

---

## 2. `https://www.googleapis.com/auth/gmail.send`

> LINDA drafts suggested follow-up emails from a conversation. When the user
> reviews and approves a draft, LINDA sends it from their connected mailbox.
> `gmail.send` is used **only** to send a message the user has explicitly
> composed or approved in the product; LINDA does not send mail automatically
> or in the background. We chose `gmail.send` specifically because it is
> send-only and grants no read access to the mailbox.

**Why this scope:** `gmail.send` is the narrowest scope that can send a
message. It cannot read, list, or modify mail, so it is the least-privilege
choice for the "send the approved follow-up" feature.

---

## 3. `https://www.googleapis.com/auth/calendar.events`

> When a user schedules a follow-up from a conversation (for example, booking
> the next call), LINDA reads and creates the corresponding event on their
> calendar. `calendar.events` is limited to event data the app creates or that
> is relevant to the follow-up; it does not grant access to calendar settings
> or sharing configuration.

**Why this scope:** `calendar.events` is narrower than full
`https://www.googleapis.com/auth/calendar`; it covers only events, which is
exactly the follow-up-scheduling feature and nothing more.

---

## 4. `https://www.googleapis.com/auth/contacts.readonly`

> LINDA matches the people in a conversation to the correct customer record so
> that insights are attributed to the right contact. `contacts.readonly` is
> used to read contact identity (name, email) for that resolution. It is
> read-only — LINDA never edits the user's contacts.

**Why this scope:** the read-only contacts scope is the least privilege that
supports identity resolution; the writable `contacts` scope is not needed
because LINDA never modifies contacts.

---

## Notes for the reviewer-facing narrative

- All four scopes map to **visible, demonstrable features** in the product; the
  demo video walks through each one (see [`demo-video-script.md`](demo-video-script.md)).
- The restricted scope (`gmail.readonly`) is governed by the **Limited Use**
  commitments documented in [`../limited-use-compliance.md`](../limited-use-compliance.md)
  and restated in the public [privacy policy](https://lindaai.net/privacy):
  data is used only to provide the user-facing analysis feature, is not sold,
  is not used for ads, and is **not used to train generalized or
  non-personalized AI/ML models**.
