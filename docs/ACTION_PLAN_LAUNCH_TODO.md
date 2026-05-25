# Action Plan — pre-launch TODO

Tracked work that depends on real client accounts / production
credentials and is therefore deferred until we have a tenant who needs
each piece. Each item is wired into the code today but won't be
end-to-end verified until the corresponding integration is connected.

The action plan feature works WITHOUT any of these — the synthesizer
runs against the LLM only, plans render in the UI, the engine state
machine works on manual notes, and Linda can create 1-step plans via
chat. These TODOs are about the integrations that *extend* the
feature beyond manual operation.

---

## TODO-1: CRM live-refresh verification (HubSpot / Salesforce)

**Status:** Wired but unverified against a real CRM account.

**What's done:**

- `backend/app/services/crm/sync_service.py:refresh_customer_deals()`
  streams the connected provider's deals and upserts matches for one
  customer. Uses existing adapters (HubSpot / Salesforce / Pipedrive)
  and the existing `CrmDealRecord` cache.
- `backend/app/services/action_plan/external_context.py` calls
  `refresh_customer_deals` when the cached `last_synced_at` is older
  than 15 min (the locked freshness window). Falls back to stale
  cache on any live-fetch failure and marks the snapshot stale so the
  plan header can surface it.
- The capability gate (`build_capabilities_block`) already filters
  procedure steps that target unconnected providers — proven by the
  unit tests.

**What's NOT done:**

- End-to-end verification with a real HubSpot or Salesforce sandbox.
  The adapters are exercised today by the nightly tenant sync but the
  per-customer hot path is fresh code.
- Confirmation that the customer external-id lookup
  (`Customer.metadata_['crm_external_ids'][provider]`) is populated
  by the existing sync pipeline. If it isn't, `refresh_customer_deals`
  no-ops and we always serve cache.

**Verify when:** the first client with a connected HubSpot or
Salesforce account starts using the action plan UI.

**Verify how:** Look at the plan header — if it shows
"CRM data stale" persistently after a brand-new call, the live
refresh isn't landing. Check `CrmDealRecord.last_synced_at` for that
customer to confirm.

---

## TODO-2: Email send/receive RFC 822 matching verification

**Status:** Wired but unverified against real outbound + inbound flow.

**What's done:**

- `POST /action-plans/{id}/steps/{id}/sent` records the outbound
  `provider_message_id` on a `StepResponse` row.
- `action_plan_match_inbound_email` Celery task fires on every inbound
  email Interaction, walks `In-Reply-To` + `References` headers, and
  matches against open steps. RFC 822 logic is covered by unit tests.
- Call D (extractor) runs against the inbound body once a match lands,
  with quoted-history stripping.

**What's NOT done:**

- End-to-end verification with a real Gmail / Outlook OAuth account.
  The matcher works on synthesized headers in tests; we haven't
  exercised it against a genuine reply chain from Gmail.
- Frontend doesn't yet call `POST /sent` after a successful email
  send (the existing email composer was built before the action plan
  feature). The composer needs a small follow-up: on successful send,
  if the email was opened from an action step's draft, POST the
  `provider_message_id` back to attach.

**Verify when:** the first client uses the canvas to send an email from
a step and receives a reply.

**Verify how:** Send an email from an action step's draft via the
composer, get the recipient to reply, then check the step's
`responses[]` for an `inbound_email` row with non-empty
`extracted_data`. If the inbound landed but didn't match, the
`outbound_message_id` wasn't recorded by the composer.

---

## TODO-3: Voyage embeddings for KB orchestrator + retrieval

**Status:** Wired; needs API key.

**What's done:**

- KB orchestrator parses documents into typed chunks; chunks are
  embedded via the existing Voyage embedder.
- `ActionPlanRetriever` does vector search via the existing
  `PgVectorStore` / `QdrantStore` abstraction.

**What's NOT done:**

- `VOYAGE_API_KEY` is not set on staging (was deferred during the
  initial action-plan build). Until set, KB ingest will fail when
  the orchestrator tries to embed chunks. Plans will still synthesize
  but with no retrieved procedures or context — every plan will be
  "AI-suggested only" with no KB grounding.

**Verify when:** before the first client uploads a real KB document
that should drive procedure-backed plans.

**Verify how:** Upload a KB document via the admin UI. Watch the
Celery worker logs for the orchestrator task — should produce
"Orchestrated doc <id>: N chunks (procedure=X, context=Y, ...)".
If it errors with a Voyage auth message, the key isn't set.

---

## TODO-4: Linda canvas affordances (deferred polish)

**Status:** Functional but spartan.

**Known gaps in the canvas UI:**

- Slot override is wired in the backend (`POST /override`) but the
  current step card doesn't expose an inline edit affordance for
  extracted slot values. Agents see the chip but can't click-to-edit
  yet.
- No SSE subscription on the canvas page — the React Query cache
  invalidates on user actions but won't auto-refresh when an inbound
  email lands while the page is open. Polling fallback (the existing
  `/notifications/stream` infrastructure handles this if subscribed).
- Diff view between artifact versions ("compare to previous") is not
  built. The data is there (`step_artifacts` is append-only).

**Verify when:** after first real-client usage surfaces which gaps
matter most.
