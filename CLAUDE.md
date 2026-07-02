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

This is advisory (~70% adherence); the real enforcement is the `model:` pinned on
each subagent in `.claude/agents/`. Default to the cheap tier and escalate
deliberately:

| Task | Use | Why |
|---|---|---|
| Search, inventory, "where/how does X work", map the codebase | **`explore` subagent (Haiku)** | Read-only; keeps exploration out of the main context (context-rot control); cheapest tier. |
| Well-specified edits, refactors, writing tests, reviewing a diff | **`implement` subagent (Sonnet)** | Routine implementation the mid tier handles well and cheaply. |
| Hard architecture / multi-file change plans / tricky trade-offs | **`design` subagent (Opus)** or the main Opus session | Reserve the top tier for genuinely hard, long-horizon work. |

- Prefer delegating read-heavy exploration to `explore` rather than reading many
  files in the main session.
- **Fable 5 is a manual, deliberate build-time choice only** — for genuinely hard,
  long-horizon work, never a default. It is capacity-constrained (~2× an Opus call)
  and is **not** set as any subagent's model.
- Layer B config (`.claude/`) must never change runtime application behavior.
