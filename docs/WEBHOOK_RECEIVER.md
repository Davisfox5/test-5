# CallSight AI Webhook Receiver — Integration Guide

This is what a team integrating a CallSight webhook subscription must
implement on their side.

## Events

Subscribe by registering a URL under `POST /api/v1/webhooks`. You choose which
events you want; the wildcard `"*"` matches every event.

Current events:

| Event                    | Fires when                                                |
|--------------------------|-----------------------------------------------------------|
| `interaction.created`    | A new Interaction row is persisted (pre-analysis).         |
| `interaction.analyzed`   | AI analysis completed and insights are stored.             |
| `interaction.consent_missing` | Ingest rejected because `consent_method` was absent.  |
| `conversation.updated`   | A message was added to an existing conversation thread.    |
| `kb.document.indexed`    | A KB document was embedded and made searchable.            |
| `quality.alert`          | LLM-judge composite score fell below threshold.            |
| `integration.token_refreshed` | An OAuth integration completed a successful refresh. |

## Request shape

Every request is a `POST` with `application/json`. The body is:

```json
{
  "event": "interaction.analyzed",
  "emitted_at": "2026-04-21T17:02:33Z",
  "tenant_id": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "interaction_id": "…",
    "channel": "voice",
    "sentiment_score": 0.72,
    "insights": { /* ... */ }
  }
}
```

Headers:

```
X-CallSight-Timestamp: 2026-04-21T17:02:33Z
X-CallSight-Signature: sha256=<64-char hex>
X-CallSight-Event:     interaction.analyzed
Content-Type:          application/json
User-Agent:            CallSight-Webhook/1.0
```

## Signature verification

Every request is HMAC-SHA256 signed. Verify in this order — **reject** if any
check fails:

1. **Timestamp window:** reject if `|now - X-CallSight-Timestamp| > 300
   seconds`. This defends against replay of captured payloads.
2. **Signature match:** compute
   `HMAC-SHA256(webhook_secret, timestamp + "\n" + raw_body)` and compare to
   the hex in `X-CallSight-Signature` using a constant-time compare.
3. **Event allowlist (optional but recommended):** reject if `X-CallSight-Event`
   is not in the set your handler knows how to process.

### Python example

```python
import hmac, hashlib, time
from datetime import datetime, timezone

WEBHOOK_SECRET = b"<secret returned from POST /webhooks>"
MAX_SKEW = 300  # seconds

def verify(headers: dict, raw_body: bytes) -> bool:
    ts_hdr = headers.get("X-CallSight-Timestamp", "")
    sig_hdr = headers.get("X-CallSight-Signature", "")
    if not ts_hdr or not sig_hdr.startswith("sha256="):
        return False
    try:
        ts = datetime.fromisoformat(ts_hdr.replace("Z", "+00:00"))
    except ValueError:
        return False
    if abs(time.time() - ts.timestamp()) > MAX_SKEW:
        return False
    expected = hmac.new(
        WEBHOOK_SECRET,
        ts_hdr.encode("ascii") + b"\n" + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_hdr.split("=", 1)[1])
```

### Node example

```js
const crypto = require("crypto");
const MAX_SKEW_MS = 300_000;

function verify(headers, rawBody, webhookSecret) {
    const ts = headers["x-callsight-timestamp"];
    const sigHdr = headers["x-callsight-signature"];
    if (!ts || !sigHdr?.startsWith("sha256=")) return false;
    const skew = Math.abs(Date.now() - Date.parse(ts));
    if (Number.isNaN(skew) || skew > MAX_SKEW_MS) return false;
    const expected = crypto
        .createHmac("sha256", webhookSecret)
        .update(`${ts}\n${rawBody}`)
        .digest("hex");
    const provided = sigHdr.slice("sha256=".length);
    if (expected.length !== provided.length) return false;
    return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(provided));
}
```

Capture the raw body (`raw_body`/`rawBody`) before JSON parsing — reading the
parsed dict and re-serializing will produce a different string and break the
signature.

## Response contract

- Return a 2xx status to acknowledge receipt. Any 2xx is treated as delivered.
- Return any non-2xx to signal failure; we retry with exponential backoff
  (`[10, 20, 40, 80, 160, 320]` seconds, max 6 attempts).
- Timeout: we close the connection after 10 seconds. Long-running handlers
  should enqueue the payload and return 200 immediately.

## Retry + replay

Failed deliveries are persisted in the `webhook_deliveries` table on the
CallSight side. A tenant admin can replay a specific delivery via:

```
POST /api/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/replay
Authorization: Bearer <admin api key>
```

## Rotating the secret

```
POST /api/v1/webhooks/{webhook_id}/rotate
Authorization: Bearer <admin api key>
```

Response body includes the new secret (shown once). We continue to accept the
previous secret for 30 minutes after rotation so subscribers can roll forward
without downtime.

## Testing your handler

Send a synthetic event with a valid signature:

```bash
curl -X POST https://your-handler.example.com/webhook \
    -H "Content-Type: application/json" \
    -H "X-CallSight-Event: interaction.analyzed" \
    -H "X-CallSight-Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    -H "X-CallSight-Signature: sha256=$(printf '%s\n%s' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "{}" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | awk '{print $2}')" \
    -d '{}'
```

If this returns 2xx, the signature verification is wired correctly.
