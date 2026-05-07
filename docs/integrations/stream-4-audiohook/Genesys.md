# Genesys Cloud AudioHook — Customer Admin Guide

This guide documents the customer-side admin steps required to enable
the LINDA AudioHook integration in a Genesys Cloud organization. It
covers the path Genesys publishes for the **AudioHook Monitor**
integration type. The newer **AudioHook Mediator** type (stream
post-call recordings rather than live audio) follows the same
auth/protocol shape but a different admin location; see the "Mediator
notes" section at the end.

## Prerequisites

Before a customer can install the LINDA AudioHook integration:

1. The Genesys Cloud org must be on a license tier that includes
   AudioHook Monitor — Genesys Cloud CX 3 or higher today, or any
   tier with the AudioHook Monitor add-on enabled.
2. AudioHook is metered per-minute by Genesys. The org admin must
   accept the metered-billing terms before the integration can stream
   any audio.
3. The recorded users must already be in a Genesys queue or division
   the admin has rights to manage. AudioHook attaches at the queue
   level (or org-wide with a checkbox) — there's no per-user toggle.
4. LINDA must have provisioned an AudioHook integration row for the
   tenant. The integration carries the tenant-side HMAC secret +
   API key (see "How LINDA provisions an AudioHook integration"
   below). Until that row exists, the upgrade signature check rejects.

## Step 1 — Open the AudioHook Monitor integration in Genesys

1. Sign in to Genesys Cloud as an admin (Master Admin or any role
   with `integrations:integration:add`).
2. Navigate **Admin → Integrations → Integrations**.
3. Click **+ Integrations** in the top-right.
4. Search for **"AudioHook Monitor"**, then click **Install**.

The integration appears in the list with status **Inactive**.

## Step 2 — Configure the integration

Open the newly-installed integration and switch to the **Configuration**
tab:

| Field | Value | Notes |
|---|---|---|
| Connection URI | `wss://<linda-host>/api/v1/audiohook/{tenant_id}` | The `{tenant_id}` is LINDA's tenant UUID — your Customer Success contact provides it. |
| Channel | `Both` (default) | `External` = customer leg only, `Internal` = agent leg only. `Both` is recommended; LINDA renders agent-vs-customer turn-taking from the channel labels. |
| Connection Probe | Enabled | Required. Genesys runs a probe call when you save the integration to verify the URL and signature. |

Switch to the **Credentials** tab:

| Field | Value |
|---|---|
| API Key | The `api_key` value LINDA provided. |
| Client Secret | The `client_secret` value LINDA provided. |

Both values are tenant-specific. Treat the secret like a database
password — anyone holding it can spoof an AudioHook session into your
LINDA tenant.

Save. Genesys runs the connection probe at this point. If you see a
"Connection probe failed" error, jump to "Troubleshooting" below.

## Step 3 — Activate the integration

Back on the integration's **Details** tab, click **Active** to turn
the integration on. The status changes to **Active** and Genesys will
begin establishing AudioHook sessions for matching calls.

## Step 4 — Bind the integration to queues

By default, an Active AudioHook integration does NOT yet stream any
calls — you have to attach it to specific queues (or the whole org).

1. Navigate **Admin → Contact Center → Queues**.
2. Open the queue you want LINDA to monitor (e.g. "Inside Sales").
3. Switch to the **Voice** tab.
4. Under **AudioHook**, select the LINDA integration from the
   dropdown.
5. Save.

Repeat for every queue you want covered. To monitor all queues, use
**Admin → Contact Center → Settings → Voice** and set the org-wide
default AudioHook integration.

## Step 5 — Verify the first session

Place a test call into the bound queue. In Genesys Cloud:

1. Navigate **Performance → Workspace → Conversations** and find the
   in-progress conversation.
2. Click **Audit Trail**. You should see an `AudioHook session opened`
   event with the LINDA integration name.

In LINDA: the call appears in the **Live coaching** view if the
AudioHook integration is the only audio path, or alongside the
existing CPaaS path if both are configured.

## Mediator notes

If your org uses **AudioHook Mediator** (post-call recording stream)
instead of the Monitor, the same credentials and Connection URI apply.
The integration lives at **Admin → Integrations → Integrations →
AudioHook Mediator** and binds at **Admin → Recording → Recording
Settings** rather than the queue level. The protocol is identical;
only the admin path changes.

## Troubleshooting

**"Connection probe failed"** in Step 2:

* Re-check the Connection URI — the `{tenant_id}` UUID must match the
  one LINDA assigned. A missing trailing path segment is the most
  common cause.
* Re-check the API Key and Client Secret. A stray whitespace
  character in the paste defeats HMAC verification.
* Verify the LINDA host is reachable from Genesys' NAT range.
  AudioHook does NOT support self-signed TLS — the host must serve a
  certificate from a public CA Genesys trusts.

**"AudioHook session opened" appears but no audio in LINDA**:

* Confirm the queue's Voice tab has the LINDA integration selected.
* Confirm the recording policy hasn't paused recording — the
  AudioHook session inherits the same policy.

**Session ends with code 1008**:

* HMAC verification rejected the upgrade. Almost always a credentials
  mismatch. Rotate the secret in LINDA and re-paste both values.

**Session ends with code 1002**:

* Protocol error — LINDA logged the specific reason. Contact LINDA
  Customer Success with the Genesys conversation id.

## How LINDA provisions an AudioHook integration

(For LINDA Customer Success / engineers — not the customer admin.)

The tenant-side row that backs the upgrade verification is an
`Integration` model row with:

```
provider = "genesys_audiohook"
tenant_id = <tenant uuid>
provider_config = {
    "api_key": "<random 32-byte API key>",
    # client_secret stored ONE of two ways:
    #   1. encrypted in `access_token` via services.token_crypto
    #   2. plain in provider_config["client_secret"]
    # (see backend/app/api/audiohook.py:_resolve_audiohook_secret)
}
```

The admin endpoint to create / rotate this row is owned by a
follow-up workstream (Stream 4 only delivers the WebSocket ingest +
verification; the per-tenant secret CRUD UI is not in scope this
round).
