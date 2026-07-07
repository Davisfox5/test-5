# Enterprise SSO setup (Okta / Microsoft Entra ID / Google Workspace)

LINDA's login is brokered through **Clerk**. We deliberately do **not**
run our own SAML/OIDC service provider — the identity federation, key
rotation, and IdP-specific quirks are handled by Clerk's enterprise SSO,
which is SOC 2 audited and cheaper and more reliable than maintaining a
bespoke SAML stack. LINDA verifies the Clerk-issued session JWT and maps
the identity onto a tenant + user.

Two capabilities combine to give a customer full enterprise SSO:

| Piece | What it does | Where it's configured |
|---|---|---|
| **Clerk enterprise connection** | The actual SAML/OIDC handshake with the customer's IdP | Clerk dashboard |
| **SCIM provisioning** *(optional)* | Push users/deprovisions from the IdP into LINDA ahead of first login | `POST /scim/v2/Users` (see below) |
| **JIT provisioning** *(optional)* | Auto-create/link a LINDA user on first SSO login | `SSO_JIT_PROVISIONING_ENABLED` + `tenants.features_enabled['sso']` |
| **Group → scope mapping** *(optional)* | Turn IdP group membership into LINDA motion scopes | `POST /admin/motion-provisioning-rules` |

You need **one** of SCIM or JIT so that SSO users actually get a LINDA
account. Most customers use SCIM (users exist before they log in); JIT is
the lighter-weight option when you'd rather create users lazily on first
login.

---

## 1. Wire the customer's IdP into Clerk

In the Clerk dashboard for the LINDA instance:

1. **Organizations → Enable organizations** (if not already on). Create an
   organization for the customer, or use their existing one.
2. **SSO connections → Add connection → Enterprise SSO.** Pick the
   protocol the customer's IdP speaks (SAML for Okta/Entra, OIDC or SAML
   for Google Workspace) and follow Clerk's wizard. You'll exchange
   metadata / ACS URL / entity id with the customer's IdP admin — Clerk
   generates these; LINDA needs none of them.
3. Restrict the connection to the customer's verified email domain(s).

At this point a user at the customer can complete the IdP login and Clerk
will mint a session JWT. LINDA still has to know **which tenant** that
identity belongs to and **which user** it is — steps 2 and 3.

### Customise the Clerk JWT template

LINDA reads these claims off the session token. Add them to the instance's
JWT template (Clerk → Sessions → Edit JWT template) so they're present:

- `org_id` — Clerk organization id (shortcode `{{org.id}}`). Primary key
  we map to a tenant. **Recommended.**
- `email` — the user's primary email (`{{user.primary_email_address}}`).
  Required for JIT *create* mode and for email-domain tenant mapping.
- `groups` — *(optional)* the IdP groups, if you want group→scope
  mapping. Clerk does not emit this by default.

---

## 2. Map the Clerk org (or email domain) to a LINDA tenant

Provisioning needs to resolve a token to exactly one tenant. That mapping
lives on the tenant row, under `features_enabled['sso']`:

```json
{
  "sso": {
    "clerk_org_ids": ["org_2ab…"],
    "email_domains": ["acme.com"],
    "jit_create": true,
    "default_role": "agent",
    "default_agent_domains": ["customer_service"]
  }
}
```

- **`clerk_org_ids`** — authoritative. The `org_id` claim is matched here
  first.
- **`email_domains`** — fallback when there's no org claim. Only list
  domains the customer has **verified** in their IdP; whoever controls the
  domain controls the tenant they land in.
- **`jit_create`** — allow creating brand-new users (see step 3). Leave
  it out/false if you provision exclusively via SCIM.
- **`default_role`** — `agent` | `manager` | `admin` for JIT-created
  users (defaults to `agent`).
- **`default_agent_domains`** — motion domains a JIT-created user starts
  with.

A token that maps to **zero or more than one** tenant is rejected
(fail-closed) — never dropped into an arbitrary tenant.

---

## 3. Choose how SSO users get an account

### Option A — SCIM (provision ahead of login)

Point the IdP's SCIM app at `POST {API}/scim/v2/Users` with a per-tenant
API key holding the `users:write` scope. Users (and deprovisions) sync
before anyone logs in; first SSO login just links the existing row by
`clerk_user_id`. This works with JIT disabled.

### Option B — JIT (create/link on first login)

Set the runtime env var:

```
SSO_JIT_PROVISIONING_ENABLED=true
```

On a net-new SSO login, LINDA will:

1. resolve the tenant from `org_id` (or email domain) per step 2;
2. **link** an existing invited/SCIM user with the same email and no
   `clerk_user_id` yet — the safe common case, no row created (it will
   never re-point an email already bound to a different Clerk identity);
3. otherwise **create** a user, but only if that tenant set
   `jit_create: true` and the token carried an `email` claim.

JIT is off by default. With it off (and no SCIM), a net-new SSO user
authenticates at Clerk but is rejected by LINDA — which is the safe
default, not a bug.

---

## 4. (Optional) Map IdP groups to LINDA scopes

If you added the `groups` claim, create rules so group membership grants
motion scopes, re-evaluated on every login:

- `GET/POST /admin/motion-provisioning-rules`
- `PATCH/DELETE /admin/motion-provisioning-rules/{id}`
- `POST /admin/sso/test-resolve` — dry-run a set of groups.

Closed by default: a user whose groups match no rule gets no scopes.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| SSO login succeeds at the IdP but LINDA returns 401 | No local user and neither SCIM nor JIT provisioned one; or the token maps to no tenant. Check `features_enabled['sso']` and `SSO_JIT_PROVISIONING_ENABLED`. |
| "no tenant mapped" in logs | `org_id`/email domain isn't listed on any tenant's `sso` config, or is listed on more than one (ambiguous → rejected). |
| JIT logs "token carried no email claim" | Add `email` to the Clerk JWT template — create-mode needs it. |
| User logs in but has no permissions | Group→scope rules matched nothing (closed by default), or no `groups` claim in the token. |

## What we intentionally did **not** build

A native, in-house SAML/OIDC service provider (ACS endpoint, IdP
metadata, per-IdP certificate handling). Clerk's enterprise SSO covers
every SAML/OIDC IdP a customer is likely to bring, at a fraction of the
build and maintenance cost, and keeps auth-critical crypto out of our
codebase. If a customer's IdP is one Clerk cannot broker, that's the
signal to revisit — not before.
