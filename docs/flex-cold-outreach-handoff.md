# Handoff prompt: build the Flex console integration for LINDA cold outreach

> Paste everything below this line into the Flex-side Claude session.

---

You are working in the **Flex** repo (gym-management SaaS, Next.js). Flex
already consumes LINDA's REST API (`src/lib/linda/client.ts`) with a tenant
API key and receives signed LINDA webhooks at
`POST https://flexonline.net/api/webhooks/linda`
(`src/app/api/webhooks/linda/route.ts`, verifying `X-Linda-Signature-V2`).

LINDA has shipped an end-to-end **cold-outreach engine** (branch
`claude/cold-outreach-campaigns-0jlb2k`, migration `out_001`). Your job:
extend the `/super-admin/prospects` console to drive it — import prospects,
create/review/approve/activate campaigns, watch the pipeline move via
webhooks, and render each prospect's interaction timeline.

## 0. Infrastructure status — read first

- **The 2026-07-09 lindaai.net 401s**: root cause was the domain being
  repointed from `linda-staging` (which holds the Flex tenant DB) to the
  new `linda-prod` app (empty DB). Remediation is operator-side (see
  LINDA's `docs/runbook.md` §11): either repoint the domain back to
  staging or migrate the tenant to prod. **Whichever option is taken,
  Flex changes NOTHING** — same base URL (`https://lindaai.net`), same
  API key, same webhook secret; they all travel with the tenant rows.
  **Interim only**: if the domain is still wrong when you start, point
  Flex's LINDA base URL env at `https://linda-staging.fly.dev`
  temporarily and revert once `dig AAAA lindaai.net` matches
  `linda-staging.fly.dev` (or the tenant is migrated).
- **API key scope**: the new mutating endpoints require the
  `campaigns:write` scope on the tenant API key (or a legacy `*` key).
  If Flex's key was minted with narrow scopes, mint/update one to add
  `campaigns:write` before wiring the console. Reads need no scope.
- **Webhook signature scheme is unchanged** (`X-Linda-Signature-V2:
  t=<unix>,v1=<hex>` over `"{t}.{raw_body}"`, ±5 min tolerance). Your
  existing verifier handles all new events. You only need to subscribe:
  `PATCH /api/v1/webhooks/{id}` with the expanded `events` list, or use
  a wildcard prefix — `"outreach.*"` works, plus
  `"prospect.status_changed"`, `"campaign.completed"`,
  `"action_plan.updated"`.
- No new env vars are required on the Flex side beyond what you already
  have (LINDA base URL, API key, webhook secret).

All endpoints below are under `https://lindaai.net/api/v1` with
`Authorization: Bearer <tenant API key>`. Standard error shape everywhere:
`{"detail": "..."}` (string) or, for validation failures,
`{"detail": [{loc, msg, type}, ...]}` (FastAPI/pydantic 422).
`prospect_id` **is** the LINDA customer UUID.

## 1. Prospects

### POST /prospects/import  (scope: campaigns:write)

Bulk upsert, idempotent on (tenant, normalized website domain) — re-POSTing
the same file updates instead of duplicating. Max 500 rows per call (chunk
the ~250-row import in one call). Rows without a parseable domain fall back
to case-insensitive business-name matching. Re-imports refresh metadata and
fill missing contact fields but NEVER reset an advanced `pipeline_status`
and NEVER resurrect a do-not-contact prospect.

Request:
```json
{
  "default_source": "sweep-2026-07",
  "prospects": [
    {
      "business_name": "Iron Works Gym",          // required, 1-300 chars
      "website": "https://www.ironworksgym.com/", // any URL/domain form
      "city": "Nashville", "state": "TN",
      "segment": "boutique strength",
      "current_software": "MindBody",
      "hook": "They complain about MindBody's per-lead fees on IG",
      "notes": "Owner active on IG, posts 3x/week",  // becomes a CustomerNote
      "contact": {
        "name": "Sam Ruiz",
        "email": "sam@ironworksgym.com",           // needed to enroll in a campaign
        "phone": "+1615...", "instagram": "@ironworksgym"
      },
      "source": "ig-sweep",                        // overrides default_source
      "initial_status": "new"                      // default "new"
    }
  ]
}
```

Response 200:
```json
{
  "created": 3, "updated": 1,
  "errors": [{"index": 7, "error": "ValueError: ..."}],   // per-row, others still land
  "prospects": [
    {"prospect_id": "<uuid>", "business_name": "...", "domain": "ironworksgym.com",
     "pipeline_status": "new", "contact_id": "<uuid|null>", "created": true}
  ]
}
```

### GET /prospects

Query: `status` (pipeline value), `campaign_id`, `q` (name/domain search),
`limit` (≤200, default 50), `offset`. 422 on unknown `status`.

Response 200:
```json
{
  "items": [{
    "prospect_id": "<uuid>", "business_name": "...", "domain": "...",
    "pipeline_status": "contacted",       // new|queued|contacted|replied|demo|won|lost|do_not_contact
    "pipeline_status_changed_at": "<iso8601|null>",
    "do_not_contact": false,
    "city": "...", "state": "...", "segment": "...", "current_software": "...",
    "hook": "...", "source": "...", "instagram": "...",
    "primary_contact": {"id": "<uuid>", "name": "...", "email": "...", "phone": "..."} ,
    "memberships": [{
      "campaign_id": "<uuid>", "campaign_name": "...", "member_id": "<uuid>",
      "state": "in_sequence",             // see §2 member states
      "touches_sent": 1,
      "next_send_at": "<iso8601|null>", "last_sent_at": "<iso8601|null>"
    }],
    "last_interaction_at": "<iso8601|null>"
  }],
  "total": 250, "limit": 50, "offset": 0
}
```

### GET /prospects/{prospect_id} → one item of the same shape. 404 `{"detail":"Prospect not found"}`.

### GET /prospects/{prospect_id}/timeline

The chronological interaction tree (newest first, `limit` ≤500 default 100):
outbound campaign sends, inbound replies, calls/transcripts, notes — plus
bounce/opt-out campaign events that never became interactions.

```json
{
  "prospect_id": "<uuid>",
  "entries": [
    {"kind": "interaction", "occurred_at": "<iso>", "interaction_id": "<uuid>",
     "channel": "email", "direction": "outbound", "subject": "...",
     "snippet": "first 280 chars", "campaign_id": "<uuid|null>",
     "event_type": null, "note_id": null, "body": null},
    {"kind": "campaign_event", "occurred_at": "<iso>", "campaign_id": "<uuid>",
     "event_type": "bounce"},
    {"kind": "note", "occurred_at": "<iso>", "note_id": "<uuid>", "body": "..."}
  ]
}
```

### PATCH /prospects/{prospect_id}  (scope: campaigns:write)

Body (all optional): `{"pipeline_status": "...", "do_not_contact": bool,
"reason": "..."}`. Manual status writes may move in any direction (unlike
campaign-driven transitions, which are forward-only). Setting
`do_not_contact: true` (or status `do_not_contact`) halts every active
sequence and emits `outreach.email.opted_out` (`source: "manual"`).
Returns the prospect shape. Use this for the console's Won/Lost/Demo
buttons and the DNC toggle.

### POST /prospects/{prospect_id}/opt-out  (scope: campaigns:write)

Shortcut for `PATCH {do_not_contact: true}`. Returns the prospect shape.

## 2. Outreach campaigns

Member states: `draft_pending → needs_approval → queued → in_sequence` and
terminal `replied | bounced | opted_out | completed (sequence exhausted,
no reply) | failed (provider error) | halted (manual/DNC)`.
Campaign statuses: `draft → active ⇄ paused → completed` (`archived` reserved).

### POST /outreach/campaigns  (scope: campaigns:write) → 201

```json
{
  "name": "July gyms sweep",
  "prospect_ids": ["<uuid>", "..."],       // optional at create; max 1000
  "config": {
    "template": {
      "subject": "Quick question about {business_name}",
      "body": "Hi {business_name} — ... {hook} ...",   // placeholders: business_name, city, state, segment, current_software, hook, website
      "sender_name": "Davis Fox",                        // ┐ CAN-SPAM identity —
      "sender_business": "Flex",                         // │ all three REQUIRED
      "physical_address": "123 Main St, Nashville, TN"   // ┘ (422 without them)
    },
    "send_window": {"start_hour": 9, "end_hour": 17,
                     "timezone": "America/Chicago", "days": [1,2,3,4,5]},
    "steps": [{"offset_days": 0},
               {"offset_days": 4, "guidance": "Short, friendly bump."}],
    "daily_limit": 25,          // omit → server default 25
    "max_touches": 3,
    "mode": "review",           // "review" (human approves) | "auto" (drafts queue themselves)
    "provider": null            // null → Gmail first, then Outlook
  }
}
```

Campaign response shape (all campaign endpoints):
```json
{
  "id": "<uuid>", "name": "...", "kind": "outreach",
  "status": "draft", "config": { ...validated config echoed back... },
  "sent_count": 0, "started_at": null, "ended_at": null, "created_at": "<iso>",
  "member_states": {"draft_pending": 240, "needs_approval": 8, "in_sequence": 2},
  "quota": {"daily_limit": 25, "sent_today": 12, "remaining_today": 13,
             "tenant_daily_cap": 100, "tenant_sent_today": 12},
  "skipped": [{"prospect_id": "<uuid>", "reason": "no_contact_email"}]  // enroll results only
}
```
Enroll skip reasons: `already_enrolled | not_found | do_not_contact |
no_contact_email` — surface these after import→create so the user knows
which of the 250 need an email address.

### GET /outreach/campaigns (query `status`) → array of the shape above
### GET /outreach/campaigns/{id} → one
### POST /outreach/campaigns/{id}/members  (campaigns:write) — `{"prospect_ids": [...]}` → campaign shape (409 if completed/archived)

### POST /outreach/campaigns/{id}/generate-drafts  (campaigns:write) → 202

Body optional: `{"member_ids": ["<uuid>"]}` to target specific members.
Returns `{"status": "queued", "members_pending_draft": 240}`. Generation is
async (one Sonnet call per prospect) — **poll**
`GET /outreach/campaigns/{id}/members?state=needs_approval` and watch
`member_states` counts; expect ~1-3 s per draft to trickle in.

### GET /outreach/campaigns/{id}/members

Query: `state`, `limit` (≤500), `offset` →
```json
{"items": [{
  "id": "<member uuid>", "campaign_id": "<uuid>", "prospect_id": "<uuid>",
  "prospect_name": "...", "contact_email": "...",
  "state": "needs_approval", "current_step": 0, "touches_sent": 0,
  "next_send_at": null, "last_sent_at": null, "replied_at": null,
  "halt_reason": null,
  "draft_subject": "...", "draft_body": "...",
  "draft_status": "ready",        // null|generating|ready|approved|rejected
  "personalization": {"hook": "...", "segment": "..."}   // what the model saw
}], "total": 250, "limit": 50, "offset": 0}
```

### PATCH /outreach/members/{member_id}  (campaigns:write)

Body: `{"draft_subject"?, "draft_body"?, "action"?: "approve"|"reject"}`.
Edit+approve in one call is the review-UI path; `reject` clears the draft
back to `draft_pending` (regenerate via generate-drafts). Editing an
already-approved draft knocks it back to `needs_approval`. 409 when the
member is past the draft stage. Returns the member shape.

### POST /outreach/campaigns/{id}/approve-drafts  (campaigns:write)

`{"member_ids": [...]}` or `{"all": true}` → `{"approved": 42}`. Approves
only `needs_approval` members whose draft is `ready`.

### POST /outreach/campaigns/{id}/activate  (campaigns:write)

Validates config (422 with pydantic details, incl. missing CAN-SPAM
fields), requires a connected Gmail/Outlook OAuth integration (400 with a
clear message otherwise — deep-link the user to your existing LINDA OAuth
connect flow), auto-enqueues draft generation for members still missing
drafts, sets status `active`. Sending then happens on LINDA's scheduler
(every 10 min) inside the send window. Also resumes a paused campaign.

### POST /outreach/campaigns/{id}/pause → status `paused`, sending stops next tick.

## 3. Throttle semantics to surface in the UI

- Per-campaign `daily_limit` (default 25) AND a tenant-wide cap of 100/day
  across all outreach campaigns — the `quota` block in every campaign
  response has live counters; render "12 of 25 sent today (88 tenant-wide
  remaining)". "Today" is midnight-to-midnight in the campaign's
  `send_window.timezone`.
- Sends are spread: max 5 per campaign per 10-minute tick, weekdays/hours
  per `send_window` — an approved batch of 250 drains at ≤25/day, so show
  expected days-to-drain (`ceil(queued / daily_limit)`).
- Follow-up bumps re-enter the review queue in `review` mode (state flips
  back to `needs_approval` with a fresh bump draft) — the console's
  approval inbox is a recurring surface, not one-time. In `auto` mode
  bumps send themselves.
- A stop-keyword reply ("unsubscribe", "remove me", …) auto-marks the
  prospect do-not-contact and halts ALL their sequences; the compliance
  footer (sender identity + physical address + opt-out line) is appended
  to every send server-side — don't add your own.

## 4. Webhooks — new events on the existing channel

Names: `outreach.email.sent`, `outreach.email.replied`,
`outreach.email.bounced`, `outreach.email.opted_out`,
`prospect.status_changed`, `campaign.completed`, `action_plan.updated`.
Envelope, headers, v2 signature, retries: unchanged. Payload schemas are
documented in LINDA's `docs/webhooks.md` (§ "Cold-outreach events");
summary of what to do with each:

| Event | Console reaction |
|---|---|
| `outreach.email.sent` | bump touch count, set status chip from `pipeline_status`, prepend timeline entry (`interaction_id` provided) |
| `outreach.email.replied` | move card to Replied, show `snippet`, link `interaction_id` |
| `outreach.email.bounced` | badge the prospect, member is halted |
| `outreach.email.opted_out` | DNC badge, remove from all sending views (`source`: reply vs manual) |
| `prospect.status_changed` | move the pipeline card (`old_status` may be null; campaign transitions are forward-only) |
| `campaign.completed` | campaign summary card from `totals` |
| `action_plan.updated` | refresh the plan panel for `customer_id` |

Every payload carries `prospect_id` + names + enough state to update the
console without a follow-up fetch. Keep deduping on `X-Linda-Delivery`.

## 5. Happy-path curl walkthrough

```bash
BASE=https://lindaai.net/api/v1
AUTH="Authorization: Bearer $LINDA_API_KEY"

# 1. Import
curl -sX POST $BASE/prospects/import -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "prospects": [{
    "business_name": "Iron Works Gym", "website": "ironworksgym.com",
    "segment": "boutique", "current_software": "MindBody",
    "hook": "Hates per-lead fees",
    "contact": {"name": "Sam", "email": "sam@ironworksgym.com"}
  }]}'
# → {"created":1,...,"prospects":[{"prospect_id":"<PID>",...}]}

# 2. Create campaign (draft) with the prospect enrolled
curl -sX POST $BASE/outreach/campaigns -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "name": "July sweep", "prospect_ids": ["<PID>"],
  "config": {"template": {
    "subject": "Quick question about {business_name}",
    "body": "Hi — saw {business_name} runs on {current_software}. {hook}",
    "sender_name": "Davis Fox", "sender_business": "Flex",
    "physical_address": "123 Main St, Nashville, TN 37201"}}}'
# → {"id":"<CID>","status":"draft","member_states":{"draft_pending":1},...}

# 3. Generate drafts, poll until ready
curl -sX POST $BASE/outreach/campaigns/<CID>/generate-drafts -H "$AUTH"
curl -s "$BASE/outreach/campaigns/<CID>/members?state=needs_approval" -H "$AUTH"

# 4. Approve (bulk) and activate
curl -sX POST $BASE/outreach/campaigns/<CID>/approve-drafts -H "$AUTH" \
     -H 'Content-Type: application/json' -d '{"all": true}'
curl -sX POST $BASE/outreach/campaigns/<CID>/activate -H "$AUTH"
# → status "active"; LINDA sends inside the window; expect an
#   outreach.email.sent webhook, then (on reply) outreach.email.replied +
#   prospect.status_changed {"new_status":"replied"} at your receiver.
```

## 6. Build notes

- Reuse the existing client's timeout/error conventions; all new reads are
  fast, `generate-drafts` is the only 202-and-poll surface.
- The pipeline board maps 1:1 to `pipeline_status`; treat `do_not_contact`
  as a hard filter everywhere a send could be triggered.
- LINDA's per-customer brief now returns `outreach_recommendation`
  (`{next_step, timing, stop, demo_talking_points}`) inside the existing
  `GET /customers/{id}/brief` payload for outreach-stage prospects —
  render it on the prospect drawer next to the timeline.
- Ask-LINDA chat can propose `queue_bump_email` (confirm via the existing
  `/chat/proposals/{id}/confirm` flow you already support) — no new UI
  needed beyond rendering the proposal card text.
