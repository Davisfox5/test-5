# Complexity & Hard-Problems Register

This directory tracks the highest-difficulty engineering challenges in LINDA — the
problems that are genuinely *hard* (not merely tedious), high-stakes, and either live
today or arriving as we scale. Each has (or will have) a companion working doc that
moves from **problem statement → shared understanding → design options → chosen plan →
implementation increments**.

These are working documents. They start as an honest problem statement and grow as we
design the fix together. A challenge is not "planned" until its doc has a chosen approach
and a sequenced set of increments we both agree on.

> Provenance: distilled from a full-codebase inspection (2026-07) plus the
> [agent-infra audit](../agent-infra-audit.md) and [ARCHITECTURE.md](../../ARCHITECTURE.md).
> Every claim below is anchored to `file:line` evidence in the companion docs.

## The register

| # | Challenge | Class | Primary risk | Status |
|---|-----------|-------|--------------|--------|
| 1 | [Exactly-once correctness in the async LLM pipeline](01-pipeline-exactly-once.md) | Distributed-systems correctness | Double-charged LLM calls, orphaned interactions, crash-on-stale-loop | 🟡 Discussing |
| 2 | Soft-real-time media + live coaching under a latency budget | Real-time / concurrency | Dropped audio, misaligned diarization, stalled live coaching | ⚪ Not started |
| 3 | Model-agnostic LLM infra: cost, quality & context-rot governance | LLM infrastructure | Model-deprecation blast radius, cost/quality drift, context rot | ⚪ Not started |
| 4 | Multi-tenant isolation (89 tables) + mid-flight action-model migration | Data correctness & security | Cross-tenant leakage, non-atomic dual writes | ⚪ Not started |
| 5 | [Per-tenant ML + RAG under data scarcity (cold-start)](05-cold-start-ml-rag.md) | Statistical / retrieval | Cold-start models dark at launch; embedding drift & manual scale-out (deferred) | 🟡 Discussing |

**Honorable mention (breadth, not depth):** the external-integration surface — bidirectional
CRM sync conflict resolution, per-provider OAuth refresh, webhook idempotency. Tracked
separately if/when it earns a slot.

## Cross-cutting root cause

Four of the five challenges share one root: the system spans many independent,
failure-prone boundaries (telephony vendors, LLM providers, CRMs, tenants, model versions)
and has largely chosen **resilience-via-continue-on-error** at each seam. That keeps the
pipeline running, but converts hard failures into *silent* inconsistencies. The recurring
high-leverage move is **idempotency keys + reconciliation (detect-and-heal)** rather than
more `try/except`.

## Status legend

⚪ Not started · 🟡 Discussing / designing · 🟢 Plan agreed · 🔵 In implementation · ✅ Done
