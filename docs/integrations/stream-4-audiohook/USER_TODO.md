# Stream 4 — AudioHook USER_TODO

Items that cannot be performed by an autonomous Claude instance and
remain blockers for production rollout. Mirror of the Stream 4 row in
the master User-Only checklist
(`/Users/davisfox/.claude/plans/fair-pushback-let-s-create-playful-puddle.md`).

## Genesys-side prerequisites

- [ ] **Create a Genesys Cloud developer org (sandbox).** Sign up at
      https://developer.genesys.cloud/ and request a CX 3 sandbox
      license tier (the floor for AudioHook Monitor).
- [ ] **Validate AudioHook against the sandbox.** Live-fire test the
      LINDA integration end-to-end against a real Genesys org BEFORE
      onboarding any production customer. Stream 4 only ships
      synthetic-fixture tests; we explicitly do not connect to real
      Genesys orgs from CI.

## AppFoundry partnership (production distribution)

- [ ] **Register as a Genesys partner** at
      https://developer.genesys.cloud/partner/. The partnership
      gates AppFoundry listings and unlocks the production AudioHook
      pricing tier. Approval is typically 2–4 weeks.
- [ ] **Submit an AppFoundry listing** for the LINDA AudioHook
      integration. Required artifacts:
   - Listing description, screenshots, demo video (Genesys reviewers
     watch the video — write something the buyer sees, not internal
     positioning).
   - **Security review questionnaire** (CAIQ). Genesys reviews how
     LINDA stores the per-tenant `client_secret` and what audio
     retention LINDA performs. Stream 4 stores the secret encrypted
     via `services.token_crypto` (Fernet/AES-256) and forwards audio
     to Deepgram without persisting bytes — both points are
     defensible answers, but the questionnaire still takes weeks.
   - **Test org credentials** for the Genesys reviewer to validate
     against. Reviewers run the connection probe themselves.
- [ ] **Sign the AppFoundry distribution agreement** (revenue share +
      metered-billing terms). LINDA is on the hook for the
      per-minute AudioHook spend regardless of customer billing —
      negotiate the marketplace cut accordingly.

## Per-customer onboarding

For each LINDA customer enabling AudioHook:

- [ ] **Provision a tenant-side `Integration` row** with
      `provider="genesys_audiohook"`. The admin CRUD endpoint for
      this is a follow-up — until it lands, write the row by hand
      (an SQL one-liner is in `Genesys.md` under "How LINDA
      provisions an AudioHook integration").
- [ ] **Generate per-tenant credentials**:
   - `api_key`: 32-byte random URL-safe token. Sent in the
     `X-API-KEY` header on every upgrade.
   - `client_secret`: 64-byte random URL-safe token. Used as the
     HMAC key for signature verification. Encrypt before storing.
- [ ] **Send the customer admin** the AudioHook Connection URI
      (`wss://<linda-host>/api/v1/audiohook/{tenant_id}`), API key,
      and client secret via a secrets-handling channel (1Password
      shared item, encrypted email, etc.) — never plain Slack/email.
- [ ] **Walk the customer admin** through `Genesys.md` Steps 1–5.
      The connection probe is the single most common failure point;
      pair on the first install.

## Operational / cost decisions

- [ ] **Accept Genesys metered AudioHook billing.** Genesys charges
      per AudioHook minute at the org level; this is a cost LINDA
      bears for any customer that enables the integration. Decide
      pricing pass-through before onboarding production customers.
- [ ] **Decide on consent attestation policy.** AudioHook relies on
      Genesys' built-in consent flow; LINDA's `is_consent_attested`
      column on `audiohook_sessions` is currently always `False`.
      Either wire it from a tenant-level setting (consented by
      default) or expose a per-session toggle in the admin UI.
- [ ] **Budget WebSocket capacity.** AudioHook sessions are
      long-lived (call duration). One queue with 20 concurrent calls
      = 20 simultaneous WebSockets per LINDA host. Validate Fly's
      `performance-2x` machine ceiling before scaling beyond ~200
      concurrent sessions per host.

## Out of scope for Stream 4 (intentionally deferred)

- Replay-protection nonce cache. The auth module accepts any
  AudioHook nonce today (with a `created` skew window). Tighten
  once Genesys-side nonce reuse rates are observed in production.
- Real-time transcription QoS metrics on the AudioHook path.
  Stream 4 wires the existing Twilio Deepgram pipeline; per-provider
  telemetry comes later.
- Admin UI for AudioHook integration CRUD (create / rotate secret /
  view recent sessions). Ticket separately.
- AppFoundry listing copy + security questionnaire answers.
  Engineering can draft the technical answers; product owns the
  positioning + screenshots.

## Spec assumptions worth verifying

The Stream 4 implementation was built from the published AudioHook
Protocol spec at https://developer.genesys.cloud/devapps/audiohook/
plus engineer recall. The following points should be verified
against a live Genesys sandbox before AppFoundry submission — if any
diverge, file a follow-up to align:

1. **Signature components.** We require `@request-target`, `@authority`,
   `audiohook-organization-id`, `audiohook-session-id`,
   `audiohook-correlation-id`, `x-api-key` to all be in the signature
   base. Genesys may sign additional headers; we accept that, but if
   any required component is missing in real signatures the verifier
   will reject.
2. **L16 byte order.** We treat L16 as big-endian on the wire and
   byte-swap to little-endian for the normalizer. Spec wording
   matches but the Genesys reference clients should be confirmed.
3. **Connection probe message shape.** We accept `open` with
   `type: "connectionProbe"` and reply `opened` with empty media,
   then expect `close`. If Genesys' probe uses a distinct message
   type instead, adapt.
4. **`paused` / `resumed` direction.** We treat them as
   client→server (Genesys notifying us of a PCI pause). The spec
   also defines server-initiated `pause` / `resume` requests; we
   don't emit those today.
