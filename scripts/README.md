# Operational scripts

One-off and on-demand scripts that are **not** part of the deployed
application. Nothing here is imported by `backend/app`; each script is run
by hand (or from a workstation against staging) when needed.

| Script | Purpose | How to run |
|---|---|---|
| `smoke.py` | End-to-end smoke check against a running deployment (auth, ingest, analysis read-back). | `python scripts/smoke.py --base-url https://linda-staging.fly.dev` |
| `live_action_plan_smoke.py` | Drives the live action-plan flow end-to-end (synthesis → steps → completion webhooks). | `python scripts/live_action_plan_smoke.py` (see header for env vars) |
| `loadtest_live_paralinguistic.py` | Load generator for the live paralinguistic WebSocket path. | `python scripts/loadtest_live_paralinguistic.py` (see header) |

Backend-coupled one-offs (seeders, migration backfills) live in
`backend/scripts/` instead, because they import `backend.app` and need the
app's environment: `seed_{cs,it,sales}.py`, `seed_action_plan_demo.py`,
`seed_prompt_variants.py`, `analyze_seed.py`, `backfill_ai_trends.py`.
The seed *runner* is `backend/seed.py` (`python -m backend.seed`).
