# Agent Infrastructure Knowledge Base

> **Purpose.** A durable reference on AI agent **harnesses**, **routers/gateways**, and **agentic feedback loops** — the concepts, best practices, failure modes, and an audit checklist. Drop this into a repo so both humans and Claude Code have a shared conceptual framework before running the agent-infrastructure audit.
>
> **How to use.** Keep at `docs/agent-infrastructure.md`. Reference it from `CLAUDE.md` (e.g. "For agent/LLM-infra decisions, follow docs/agent-infrastructure.md"). It is written principles-first; anything perishable (model names, prices, vendor tools) is quarantined in §9 — do not hardcode those into design decisions.

---

## Core mental model

Three nested layers:

- **Harness** — the whole exoskeleton around the model: the code, config, and execution logic that is *not* the model. It's what turns a token-predictor into a system that acts.
- **Router** — one component *inside* the harness that decides which model (or path) handles a request.
- **Feedback loops** — the control structures *inside* the harness that let the agent check its own work and iterate.

The recurring analogy: **the model is the engine; the harness is the car.** A great engine with no chassis, brakes, or steering takes you nowhere. Corollary: **an agent = a model + a harness.** Most of the engineering value — and most of the failure risk — lives in the harness, not the model.

---

## 1. Harnesses

**Definition.** The software infrastructure wrapping an LLM that lets it *act on* tasks rather than just respond. It sits between the user and the model, managing the conversation loop, routing tool calls, maintaining state across turns, enforcing budgets, and validating outputs.

**The six components (plus a platform layer).** A production harness is usefully decomposed into:

1. **State & persistence** — durable execution and recovery, so a crashed or paused run resumes correctly.
2. **Security & governance** — identity propagation, permissions, audit trails.
3. **Orchestration & tool use** — planning loop, tool registry/schemas, tool-call routing.
4. **Memory** — tiered: working (in-context, kept ruthlessly small), episodic (session-scoped, retrieved on demand), semantic (cross-session, long-term). Prefer async memory writes so they don't block the response path.
5. **Observability** — traces of every LLM call, tool invocation, and step.
6. **Evals** — offline and online quality measurement (see §6).

Underneath these sits the **platform layer** (durable state, cost attribution, recovery logic) that separates a demo from something that survives production.

**Two reasons the harness matters more than the model:**

- **Model-agnosticism.** Tool integrations, memory, and business logic live in the harness, so you can swap the underlying model *without rebuilding the system*. This is the direct defense against a model being deprecated or suspended out from under you.
- **Measured lift.** Modular-harness research (ICML 2025 modular-harness work) reports the *same* model, unchanged, performing better *with* a well-structured harness than without — no weight or prompt changes. (Treat the specific citation as unverified; see §9.)

**Design principle.** Keep the harness the stable, model-independent interface. As frontier models improve, scaffolding for old capabilities shrinks and scaffolding for new capabilities grows — but the harness seam is what lets you absorb that churn cheaply.

---

## 2. Routers & gateways

**Router vs gateway.**
- A **router** picks *which model* handles a request (route the cheapest model that can do the job; send routine work to small models, hard reasoning to frontier).
- A **gateway** wraps the router with the operational layer: one unified API across providers, plus failover, load balancing, caching, cost tracking, and observability.

**The routing design space (three axes):**
1. **When** the decision is made — before the request (on query features), during inference, or after a first response (cascade/escalation).
2. **What** information feeds it — query features, model metadata, or past measured performance.
3. **How** it's computed — static rules, a classifier, reinforcement learning, or a cascade.

**What routing buys you.** Meaningful cost reduction while preserving quality, because most production queries are not hard. A well-designed router can also *raise* quality above any single model by exploiting each model's specialized strengths — routing is not only a cost play.

**Best practices:**
- **Tie routing to measured quality, not intuition.** Route on accuracy, cost, latency, and review signals from *real traffic*, with a pre-merge quality gate so savings never degrade answers.
- **Keep the plumbing connected.** If the router and eval system are separate, preserve request IDs, metadata, traces, and scores end-to-end so any routing change can be traced back to a quality outcome.
- **Make model choice a runtime decision, never a hardcoded constant.** Put every model call behind a single routing/config seam with an explicit fallback path.
- **Escalate, don't over-provision.** Start on a cheap tier; escalate to a stronger model only when a confidence/quality signal warrants it.

**Pitfalls & limits:**
- **The latency objection is usually a misframing** — the router isn't the bottleneck; the model it selects is. The *exception* is a router that itself calls an LLM to classify difficulty (a full extra round-trip) — reserve that for genuinely hard routing decisions.
- **Routing math is perishable.** Provider prices move frequently; re-derive any savings estimate against current pricing before budgeting.
- **A router adds a failure surface.** It needs its own health checks, timeouts, and a default-safe fallback when a provider blinks.

---

## 3. Feedback loops (the core of "agentic")

**The defining shift.** Standard inference is one-shot: input in, output out. A feedback loop makes the agent both **executor and critic**, iterating before it commits.

**The canonical pattern — Reflection / evaluator-optimizer.** A `generate → critique → revise` loop with three roles:
- **Generator** drafts the output.
- **Critic** scores it against a rubric, test, or retrieved source.
- **Reviser** fixes each flagged issue, one at a time.

Repeat until it passes or hits an iteration cap.

### The single most important rule: ground the critique

Naive self-critique fails because of the **coherence trap** — one model that both writes and judges shares its own blind spots, so it rates coherent-sounding errors as correct. Ungrounded self-correction can *lower* accuracy, and sycophancy can talk a model out of a right answer.

**Fix:** make the critique concrete and external. Run the tests. Re-query the source. Recompute the number. Compile the code. Produce *named* feedback ("line 42 won't compile") the reviser can act on. A grounded loop improves; an ungrounded one can spiral.

### Other loop discipline

- **Cap iterations.** Most gains land in the first one or two passes; further loops mostly add latency and cost. Auto-terminate if the score doesn't improve between iterations, so the agent can't spiral deeper into a bad path.
- **Two loop types, kept separate.** *In-task* loops (OODA-style: observe → orient → decide → act) govern a single attempt. *Cross-attempt* loops (Reflexion-style) turn past failures into procedural knowledge for the next attempt. Don't conflate them.
- **Separate reflection from execution.** Keep self-improvement/measurement isolated from live business execution so the loop can't corrupt production data or let the agent game its own metrics.
- **Reflections age.** A reflection captured against one schema/API/environment can become misleading after things change. Timestamp and scope them; don't let recency bias override proven strategies.

### The org-level loop — the eval flywheel

Beyond the in-task loop, mature systems run a data flywheel: production traces → evaluation → dataset/regression coverage → improved routing/prompts/tools → better production traces. This connects observability, evaluation, and iteration into one continuous workflow, so every production interaction becomes an improvement signal.

---

## 4. Workflows vs agents (decision discipline)

**Definitions:**
- **Workflow** — LLMs and tools orchestrated through *predefined code paths*. You own the control flow. Predictable, testable, cheaper.
- **Agent** — the model *dynamically directs* its own process and tool use. You own the goal and guardrails, not every branch. Flexible, but higher latency, cost, and error-compounding risk.

**Default to the simplest thing that works.** Add agency only when flexibility genuinely outweighs its costs. Many "agent" problems are better solved as a workflow.

**The five composable patterns** (learn these before reaching for full autonomy):
1. **Prompt chaining** — decompose into fixed sequential steps.
2. **Routing** — classify input, dispatch to a specialized path/model *(this is your router — a first-class pattern, not an add-on)*.
3. **Parallelization** — split work across parallel calls, then merge.
4. **Orchestrator-workers** — a lead plans and delegates subtasks, then synthesizes.
5. **Evaluator-optimizer** — generate, evaluate against criteria, refine *(this is your feedback loop — also first-class)*.

**Framework caution.** Frameworks add abstraction that can obscure the underlying prompts and responses, making debugging harder and tempting needless complexity. Prefer calling model APIs directly for core paths; if you use a framework, understand the code underneath it.

**Tool design.** Build a few thoughtful, high-impact tools that match your eval tasks. Prefer targeted tools (`search_contacts`) over dump-everything tools (`list_contacts`) that force the model to burn context reading irrelevant data.

---

## 5. Failure modes (design against these)

### Context rot (dominant single-agent failure)
As a session grows, accumulated tool output, history, and stale file contents dilute the signal. The agent gets less accurate → makes mistakes → corrections require more reading/searching → more noise → more errors. Failure isn't gradual; systems work for a while then **fall off a cliff**.

- **A bigger context window does not fix this.** The *effective* window (where quality holds) is often far smaller than the advertised limit. Performance degrades at every length increment, not just near the cap.
- **Countermeasure — pre-rot threshold.** Monitor token count and run a compaction/summarization cycle *before* entering the rot zone; don't wait for an API error.
- **Countermeasure — context isolation.** Offload sub-tasks to subagents with their own clean context; keep the main/orchestrating context small and high-signal.

### The four context failure modes (Breunig taxonomy)
- **Poisoning** — a hallucination/error enters context and is referenced repeatedly, compounding.
- **Distraction** — as history grows, the model reproduces past action patterns instead of synthesizing new plans.
- **Confusion** — irrelevant info in the window degrades output because the model tries to use everything it's given.
- **Pollution/clash** — redundant or contradictory content (including stale file versions) corrupts reasoning.

### Error compounding / integral windup
In an unbounded retry loop, an agent accumulates error-contaminated context from failed attempts, treats it as evidence, and *diverges* rather than converging — analogous to integral windup in control systems. The fix is not a more powerful model; it's bounding and grounding the loop (caps, fresh-context retries, external verification).

### Multi-agent coordination
Errors propagate: one agent's degraded output enters the next agent's context as ground truth. Failure-mode research across many traces attributes the large majority of multi-agent failures to **coordination and specification problems, not model capability**. Implications: isolate sub-agent context, define crisp handoff schemas, don't share one mutable context across agents.

### Reflection-specific pitfalls
- Extra inference steps add latency/cost.
- Ungrounded self-evaluation can lead the agent further astray (see coherence trap, §3).
- Over-indexing on the most recent failure causes over-correction.

---

## 6. Evaluation & observability

- **Deterministic where you can, LLM-judge where you must.** Use exact/deterministic checks for things like tool-call correctness; reserve LLM-as-judge for criteria needing judgment.
- **Trajectory evals > output-only for agents.** Score the spans *inside* a run (tool calls, retrieval, reasoning, handoffs) so a failed score points to the exact step that caused it.
- **Treat the judge as production code.** Write explicit evaluation steps, prefer narrow binary pass/fail over vague quality scores, split complex logic into a DAG, and calibrate judge scores against human annotations.
- **Observability underpins everything.** You cannot manage what you don't measure; guardrails catch acute real-time risks, evals/observability catch chronic drift.

---

## 7. Applying this with Claude Code (repo specifics)

Claude Code is itself a harness; its primitives map to the concepts above:

- **`CLAUDE.md`** — the repo's persistent conventions/context. Followed most of the time but not deterministically (~70% adherence in practice) — good for guidance, not for hard guarantees.
- **Hooks** — deterministic enforcement (~100%). Use `PreToolUse` hooks to block dangerous actions (e.g. destructive commands, pushes to protected branches) and `PostToolUse` for lint/format. **Put hard rules in hooks/settings, not in prose in `CLAUDE.md`.**
- **Subagents** — isolated context for read-heavy or specialized tasks; the primary lever against context rot. Use a read-only explore subagent for mapping a codebase. Subagents can also be pinned to a specific model (cheap model for read-only exploration, stronger model for hard implementation), which is the main build-time cost-control lever.
- **Plan mode** — separate exploration/planning from execution to avoid solving the wrong problem; pour effort into the plan so implementation can be near-one-shot.
- **TDD is the strongest feedback loop.** Write tests first → confirm they fail → commit them → implement until green → *do not modify tests to pass*. Each red-to-green cycle is unambiguous grounded feedback. Committing tests first means any tampering shows in the diff.
- **Close the loop with verification.** Give the agent a way to check its own work (test suite, build, browser, simulator) and it will iterate until correct. Require *evidence* (test output, the command and its result) rather than an assertion of success.
- **Review with a second agent.** Have a separate subagent review the diff against the plan; instruct it to flag only correctness/requirement gaps, not style, to avoid over-engineering.

### Two distinct layers — don't conflate them

- **Runtime (in-application) routing** — how the shipped app selects and calls a model in production. This must be correct.
- **Build-time routing** — how Claude Code spends tokens while developing the repo. This should be cost-efficient (cheap models for exploration, expensive models reserved for hard work). Build-time config lives under `.claude/` and must never change runtime application behavior.

### Why this matters in these repos

These applications (Flex, LINDA, R3CRUIT3R) make LLM API calls at runtime using Claude Opus, Sonnet, or Haiku, selected by task, preferring the current version of each tier. They do **not** call Fable 5 or any Mythos-class model at runtime — Fable 5 is used only as a build-time tool in Claude Code. Keep that separation in mind: runtime routing is about Opus/Sonnet/Haiku; Fable's cost and availability only affect build-time work.

The motivating risk is real regardless: model versions get deprecated, and models can be suspended (the June 2026 ecosystem-wide Fable 5 suspension is the salient recent example — even though it did not affect these apps, which don't call Fable). Any touchpoint pinned to a single hardcoded model/version is exposed the moment that identifier goes away. The lessons above are therefore concrete, not academic:

- Every model identifier should sit behind a **routing/config seam** with a fallback, so a deprecation/suspension is a config change, not an outage.
- Model choice should be **tiered across Haiku/Sonnet/Opus** by task difficulty — each touchpoint on the cheapest tier that meets its quality bar.
- Any self-check or retry logic should be **grounded and bounded**.
- Long-running LLM interactions should have a **context/compaction strategy**.

---

## 8. Audit checklist (drop-in)

Use this to review any app in the repo family.

**Harness presence**
- [ ] Is there a defined seam between the model and the rest of the app, or are model calls scattered inline?
- [ ] Which of the six components exist vs are missing: state/persistence, security/governance, orchestration/tool-use, memory, observability, evals?

**Routing / model-agnosticism**
- [ ] List every hardcoded model identifier with `file:line`.
- [ ] For each, what is the blast radius if that model is deprecated/suspended?
- [ ] Is there a fallback/failover path? A single config point to switch models?
- [ ] Is model choice a runtime decision or a compile-time constant?
- [ ] Is each touchpoint on the cheapest Claude tier (Haiku/Sonnet/Opus) that meets its quality bar, or is a heavy model used where a lighter one would do?

**Feedback loops**
- [ ] Locate every self-check / retry / validate-then-revise loop.
- [ ] Is each **grounded** (checked against tests/execution/retrieval/real data) or **ungrounded** (model judging itself)?
- [ ] Does each loop have an **iteration cap** and **improvement-based termination**?
- [ ] Is reflection/measurement isolated from live execution?

**Context management**
- [ ] Where does context accumulate (tool output, history, file contents)?
- [ ] Is there a pre-rot threshold / compaction / summarization step for long sessions?
- [ ] Are sub-tasks isolated into subagents to protect the main context?
- [ ] Any risk of stale file versions or poisoned context persisting across turns?

**Evals / observability**
- [ ] Are runs traced (LLM calls, tool calls, steps)?
- [ ] Are there deterministic checks for tool correctness and judge-based checks for quality?
- [ ] Is there any production→eval flywheel, or is quality unmeasured?

**Build-time (Claude Code) cost control**
- [ ] Do project-level subagents in `.claude/agents/` pin cheap models to read-only/exploration work?
- [ ] Is expensive-model use (Opus, and manual Fable) reserved for genuinely hard tasks?
- [ ] Does `CLAUDE.md` document the dev-time model convention?

---

## 9. Time-sensitive facts — verify before relying

The concepts above are durable. The following churn and must **not** be hardcoded into design decisions or treated as current without checking a primary source:

- **Specific model names, versions, context limits, and prices** — these change frequently across all providers. Re-verify against the provider's current pricing/model page before budgeting or pinning.
- **"Effective context window" thresholds** — model-specific and shift with each release; measure for the model you actually use rather than assuming a fixed number.
- **Vendor/tool lists** (routers, gateways, eval/observability platforms) — the landscape moves fast; treat any named tool as a starting point for evaluation, not a recommendation, and confirm it still fits.
- **Reported cost-savings percentages** — illustrative only; recompute against your own traffic mix and current rates.
- **The measured-harness-lift citation (§1)** and the multi-agent coordination-failure statistic (§5) — verify the underlying source and its exact claim before citing either in external-facing material.

---

## Sources & further reading (verify currency)

- Anthropic — *Building Effective Agents* (workflows vs agents; the five patterns; augmented LLM).
- Anthropic — *Writing effective tools for agents* (tool design; eval-driven iteration).
- Anthropic — *Claude Code best practices / power-user docs* (plan mode, subagents, hooks, verification, TDD).
- Chroma — *Context Rot* research (performance degradation as input length grows).
- Breunig — context failure taxonomy (poisoning, distraction, confusion, clash).
- MAST — multi-agent system failure taxonomy (coordination-dominated failures).
- ICML 2025 — *General Modular Harness for LLM Agents* (measured harness lift) — **citation unverified; confirm before relying.**
- Reflexion / evaluator-optimizer literature (generate→critique→revise; grounded vs intrinsic correction).
- 2026 surveys on dynamic LLM routing & cascading (the when/what/how design space).

> Maintenance note: review §9 and the Sources list each quarter or whenever a model you depend on changes. The rest of this document should remain valid across model generations.
