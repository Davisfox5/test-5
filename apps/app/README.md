# LINDA — Tier 2 / Tier 3 SPA

Serves both the **sandbox** (14-day trial) and the **paid production app**.
The sandbox is the same codebase running against a tenant where
`plan_tier === "sandbox"`; everything else is feature-flag-gated by the
server via `/api/v1/me` and enforced server-side by `require_feature`.

## Stack

- Next.js 15 (App Router) + React 19
- TypeScript
- Tailwind v3, colors/radii/shadows wired to
  `website/css/tokens.css` so the SPA, the public demo, and the
  Ask-Linda widget all share one design token file
- Clerk for auth (JWT → passed as `Authorization: Bearer clerk_<id>` to
  the FastAPI backend)
- TanStack Query for server state

## Local dev

```bash
cp .env.example .env.local
# fill in your Clerk keys, start the FastAPI backend on :8000, then:
npm install
npm run dev      # http://localhost:3001
```

`next.config.mjs` proxies `/api/*` to `LINDA_BACKEND_URL`.

## Routes

- `/sign-in`, `/sign-up` — Clerk-hosted
- `/signup` — company + email form that calls
  `POST /api/v1/trial/signup` to create a sandbox tenant
- `/(app)/dashboard` — authenticated shell; shows plan + trial state
- `/(app)/interactions`, `/(app)/action-items`, `/(app)/settings` —
  placeholder pages to port from `website/demo.html`

## What's scaffolded vs. what's next

**Scaffolded**: auth, routing, app shell, `useMe()`, `useFeature()`,
role-aware sidebar, trial banner, brand components.

**Next**: port the 8 demo views from `website/demo.html` into real
pages with live data; wire `useFeature("live_coaching")` guards around
the Tier 3-only surfaces; add the executive role's tenant-admin view.
