# Flex console integration hardening — LINDA-side work items

**Origin:** A 2026-07-02 bug sweep of the Flex platform (the gym-SaaS repo whose
`/super-admin/prospects` console dogfoods LINDA) fixed everything fixable on the
consumer side. The items below are the remainder: they need changes in THIS
repo (the LINDA backend on Fly, `linda-staging`, behind `https://lindaai.net`).

Flex's consumer code, for contract reference:
- Webhook receiver: `src/app/api/webhooks/linda/route.ts` (Flex repo) — verifies
  `X-Linda-Signature: sha256=<hex>` as HMAC-SHA256 over the raw body,
  timing-safe, and now tolerates signed non-object JSON by ACKing with 200
  `{ok, ignored}`.
- API client: `src/lib/linda/client.ts` (Flex repo) — 15s/45s request timeouts
  that now cover response bodies, and a 120s hard deadline on the `/ask` SSE
  stream.
- Follow-up send: Flex treats a 2xx response with `error` set or
  `status ∈ {failed, error}` as a failure (returns 502 to its UI).
- Outcomes: Flex currently smuggles the prospect id into `event_id`
  (`flex-<prospectId>-<interactionId>-<outcomeType>`) and `metadata.prospect_id`
  because `OutcomeEvent` has no customer field.

---

## 1. Webhook replay protection (highest priority)

**Problem.** Outbound webhook deliveries
(`backend/app/services/webhook_dispatcher.py`, ~line 339) sign only the raw
body: `X-Linda-Signature: sha256=<HMAC-SHA256(secret, body)>`. There is no
timestamp or nonce in the signature base, so anyone who captures one legitimate
delivery (proxy logs, APM, etc.) can replay it forever and it will verify.

**Fix.** Include a signed timestamp:
- Add an `X-Linda-Timestamp: <unix seconds>` header.
- Compute the signature over `f"{timestamp}.{body}"` and send it in a
  versioned scheme alongside the legacy one, Stripe-style:
  `X-Linda-Signature: t=<unix>,v1=<hex>` (or a separate `X-Linda-Signature-V2`
  header — pick one and document it).
- **Migration:** keep sending the legacy body-only signature during a
  transition window so existing consumers (Flex) don't break; coordinate the
  Flex receiver update, then drop the legacy header.
- Document a recommended verification tolerance (±5 minutes).

**Acceptance:** a replayed delivery with a stale timestamp fails verification
on a compliant consumer; docs/webhooks documentation updated; Flex receiver
updated in the same window (separate Flex-side PR).

## 2. Follow-up send: failure must be programmatically detectable

**Problem.** `backend/app/api/emails.py` `send_follow_up` (~line 391) writes a
pending `EmailSend` row, calls the provider, and updates the row to
`sent`/`failed`. Confirm and pin down what the HTTP response looks like when
the provider fails: if it can be a 2xx with a soft-failure body, consumers can
tell users "sent" when it wasn't.

**Fix.** Guarantee one of (and document it):
- provider failure → non-2xx (502/424 with the provider error), or
- 200 with `status: "failed"` and a non-null `error` string — never a 2xx
  `status: "sent"` unless the provider accepted the message.

Add a test for the provider-failure path asserting the response contract.

## 3. First-class customer id on outcome events

**Problem.** `OutcomeEvent` (`backend/app/api/outcomes.py`, ~lines 77–91) keys
on `interaction_id` only. Consumers attributing an outcome to a customer/deal
have no field for it — Flex packs the prospect id into `event_id` and
`metadata.prospect_id`.

**Fix.**
- Add optional `customer_id: Optional[uuid.UUID]` to `OutcomeEvent`.
- When present, validate the interaction actually belongs to that customer;
  reject (422) on mismatch instead of silently mis-attributing.
- Persist it on the outcome row (migration) so calibration/reporting can
  aggregate per customer without joining through interactions.
- Keep `event_id` idempotency semantics unchanged.

## 4. SSE chat stream termination guarantees

**Problem.** The `/ask`-style chat SSE stream can, on upstream LLM stalls or
worker errors, stop emitting without a terminal event, leaving consumers to
hang until their own deadline (Flex now enforces a 120s client-side abort).

**Fix.**
- Always emit a terminal SSE event (`done` or `error`) on every exit path,
  including exceptions and cancellations.
- Emit a heartbeat/keepalive comment every ~15s while waiting on the LLM so
  intermediaries don't idle-close the connection.
- Bound total stream lifetime server-side (e.g. 120s) with a clean `error`
  event on expiry.

## 5. Webhook payload shape guarantee

**Problem (low).** Nothing in the dispatcher's type signature prevents a
delivery whose JSON body is `null`, a bare array, or an object without a
string `event` field. Flex guards against this now (ACKs and ignores), but the
contract should be enforced at the source.

**Fix.** Assert at enqueue time that every webhook payload serializes to a JSON
object with a string `event` and an object `data`; reject/log otherwise. Add a
schema note to the webhook docs.

## 6. Verify: datetime serialization is timezone-aware everywhere (likely fine)

Model columns broadly use `DateTime(timezone=True)` (~173 occurrences in
`backend/app/models.py`), so API responses should already carry explicit
offsets. Audit the exceptions: any response field built from `datetime.utcnow()`
/ naive parsing (e.g. `OutcomeEvent.occurred_at` accepts naive input) or ad-hoc
`isoformat()` calls on naive values. A naive ISO string is parsed as LOCAL time
by browsers, shifting every displayed timestamp by the viewer's UTC offset.
Normalize any stragglers to UTC-aware before serializing.

## 7. Optional: customers list pagination ergonomics

`GET /customers/list` caps at 100 per call. Flex now pages through up to 1,000
sequentially. If pipelines grow, consider a higher max page size or cursor
pagination so consumers don't need N round trips. Low priority.

---

### Suggested order

1 (replay protection) → 2 (send contract) → 3 (outcome customer_id) → 4 (SSE)
→ 5 (payload guarantee) → 6 (datetime audit) → 7 (pagination).

Items 1 and 3 change the wire contract consumed by Flex — flag them for a
coordinated Flex-side follow-up (the Flex changes are small: verify the new
signature scheme; pass `customer_id` instead of metadata smuggling).
