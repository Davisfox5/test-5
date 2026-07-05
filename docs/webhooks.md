# Outbound webhooks — delivery contract

LINDA delivers events to tenant-registered webhook URLs (managed via
`/webhooks`, see `backend/app/api/webhooks.py`). This documents the wire
contract consumers verify against. Dispatcher implementation:
`backend/app/services/webhook_dispatcher.py`.

## Payload shape

Every delivery body is a JSON **object** with this envelope — enforced at
enqueue time (payloads that would violate it are rejected and logged, never
delivered):

```json
{
  "event": "customer.churned",          // always a non-empty string
  "tenant_id": "<uuid>",
  "emitted_at": "2026-07-02T18:00:00+00:00",
  "data": { ... }                        // always a JSON object
}
```

Consumers can rely on `event` being a string and `data` being an object; a
`null`, bare-array, or event-less body is a contract violation on our side.

## Headers

| Header | Meaning |
|---|---|
| `X-Linda-Event` | Event name (same as `event` in the body). |
| `X-Linda-Delivery` | Delivery row UUID — stable across retries; use it for idempotency. |
| `X-Linda-Attempt` | 1-based attempt counter. |
| `X-Linda-Signature` | **Legacy** signature: `sha256=<hex>` (see below). |
| `X-Linda-Timestamp` | Unix seconds at which this attempt was signed. |
| `X-Linda-Signature-V2` | **Replay-protected** signature: `t=<unix seconds>,v1=<hex>`. |

## Signature verification (v2 — use this)

1. Parse `X-Linda-Signature-V2` into `t` (integer unix seconds) and `v1` (hex).
2. Reject if `|now - t|` exceeds your tolerance. **Recommended tolerance:
   ±5 minutes** — wide enough for retries in flight and modest clock skew,
   tight enough that a captured delivery can't be replayed later.
3. Compute `HMAC_SHA256(secret, "{t}.{raw_body}")` over the **raw request
   bytes** (do not re-serialize the JSON) and compare against `v1` with a
   timing-safe comparison.

The timestamp is bound into the signed string, so an attacker who captured a
legitimate delivery cannot advance `t` without invalidating `v1`.

Each attempt (including retries) is signed fresh at send time, so retried
deliveries carry a current timestamp and pass the tolerance check.

## Legacy signature (deprecated — migration window only)

`X-Linda-Signature: sha256=<hex>` where hex = `HMAC_SHA256(secret, raw_body)`.
No timestamp is bound in, so it verifies forever — i.e. it is replayable.
It is still sent so existing consumers keep working; verify v2 instead as
soon as you can. Once known consumers (Flex) are on v2, the legacy header
will be dropped.

## Retries

5 attempts with exponential backoff (10s, 1m, 5m, 30m, 2h), then the
delivery is dead-lettered. Any 2xx acknowledges a delivery. Dedupe on
`X-Linda-Delivery` if your handler is not idempotent.

## Test pings

`POST /webhooks/{id}/test` sends a `webhook.test` event carrying the same
dual-signature headers as real deliveries, so you can verify a v2
implementation end-to-end before real traffic depends on it.
