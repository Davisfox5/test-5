# CLAUDE.md — working conventions for this repo

Architecture lives in [ARCHITECTURE.md](ARCHITECTURE.md). For agent/LLM-infra
decisions, follow [agent-infrastructure-knowledge-base.md](agent-infrastructure-knowledge-base.md)
and the audit in [docs/agent-infra-audit.md](docs/agent-infra-audit.md).

## Runtime LLM rule (Layer A — the shipped app)

- **Every runtime model id resolves through `backend/app/services/model_catalog.py`.**
  Never hardcode a `claude-*` string anywhere else. `tests/test_model_catalog.py`
  has a guard test that fails the build if you do. Bumping a version or swapping a
  deprecated/suspended model is a one-line change in the catalog / an env override.
- Runtime uses only **Haiku / Sonnet / Opus**, each touchpoint on the cheapest tier
  that meets its quality bar. **Fable (Mythos-class) is never called from app code.**
- Live model calls go through `acreate_with_failover` (in `llm_client.py`) or
  `ModelRouter` so a provider blip retries/fails over instead of failing the request.
- System Python is **3.9**: use `Optional[X]` / `Dict` / `List`, not `X | None`.
- Before pushing code that adds routers / decorators / import-time work, run
  `python3 -c "from backend.app.main import app"`.

## Model routing (dev — Layer B, how Claude Code spends tokens here)

**Scope: this section governs Claude Code working ON this repo only.** It never
applies to the application's own LLM calls — runtime model selection (Layer A) is
governed exclusively by `backend/app/services/model_catalog.py` / `ModelRouter` per
the Layer A rules above, and nothing under `.claude/` is read at runtime.

Routing is **top-down**: the highest tier does the judgment work and delegates DOWN
to cheaper tiers for mechanical work. No agent ever self-assesses its own capability
and escalates upward — escalation triggers are external only (a failing test, the
fixed sensitive-path rule below), decided by the caller.

| Agent | Model | Invoke when |
|---|---|---|
| `codebase-analyst` | fable | Architecture questions, tracing data/control flow, "why does this behave this way". |
| `code-reviewer` | fable | Reviewing a diff/PR (includes migration-safety + RLS + sensitive-path checklist). |
| `planner` | fable | Hardest refactor strategies, roadmaps, rollout sequencing (writes to `docs/` only). |
| `bug-hunter` | fable | Reproducing/localizing bugs; proposes fixes, never writes them. |
| `security-reviewer` | fable | Auditing auth/RLS/Stripe/crypto/dependency risk surface. |
| `spec-writer` | opus | Turning a fable-tier plan into a precise spec in `docs/specs/`. |
| `design` | opus | Mid-weight planning where the solution shape is mostly clear (pre-existing agent). |
| `code-writer` | sonnet | Implementing against a written spec; runs tests, shows output; stops if blocked. |
| `implement` | sonnet | Routine well-specified edits without a formal spec (pre-existing agent). |
| `researcher` | sonnet | External library/API docs, pinned-version-first. |
| `code-scout` | haiku | Pure lookups: "where is X / list call sites of Y". **Mandatory for pure search.** |
| `explore` | haiku | Read-only mapping/inventory sweeps (pre-existing agent). |

Fixed rules (external triggers, not judgment calls):

- **Sensitive-path rule:** specs and edits touching `backend/app/rls.py`,
  `backend/app/tenant_ctx.py`, `backend/app/auth.py`,
  `backend/app/api/stripe_webhook.py`, `backend/app/services/stripe_billing.py`,
  `backend/app/services/token_crypto.py`, `backend/alembic/versions/`, schema
  changes in `backend/app/models.py`, `fly.toml`, `fly.production.toml`, or
  `.github/workflows/ci-cd.yml` are authored at the **fable tier directly** —
  `spec-writer` and `code-writer` refuse those paths and report back. *Caveat: this
  trades a small fable increase for the large scout/writer reduction; if
  sensitive-path work ever dominates the workload, revisit this rule.*
- **Scout-first rule:** any pure lookup goes to `code-scout` (haiku), never to
  `codebase-analyst` or the main session. This is the main top-tier cost reduction.
- **Researcher-output-is-unverified rule:** `researcher` output is always treated as
  unverified claims; fable-tier consumers and `code-writer` re-verify against the
  pinned versions in `requirements.txt` / `apps/app/package.json` before acting.

Enforcement layers — what binds vs. what steers:

- **Mechanically enforced** (applied by the harness on every invocation): each
  agent's `model:` and `tools:` frontmatter in `.claude/agents/*.md`.
- **Advisory** (prompt/CLAUDE.md-level, ~70% adherence): whether to delegate at all,
  the scout-first habit, and path restrictions inside prompts (`planner` → `docs/`
  only, `spec-writer` → `docs/specs/` only, the sensitive-path refusals) —
  frontmatter cannot scope write paths. CLAUDE.md steers; frontmatter binds.

Other notes:

- Prefer delegating read-heavy exploration to `code-scout`/`explore` rather than
  reading many files in the main session (context-rot control).
- **Fable is a deliberate, top-down choice** — pinned on the five judgment-heavy
  agents above and invoked per this table, never as a default for mechanical work.
  It is capacity-constrained (~2× an Opus call); the tiering above exists to produce
  a net reduction in fable usage versus routing all agent work there.
- Layer B config (`.claude/`) must never change runtime application behavior.
