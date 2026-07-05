# 05 — Per-tenant ML & RAG under data scarcity (cold-start)

**Status:** 🟡 Discussing
**Owner:** _tbd_ · **Working doc — evolves as we design.**

> **Pre-launch lens (2026-07).** LINDA is pre-launch with zero tenants. That changes
> which half of this challenge matters. This doc scopes to the **cold-start / data-scarcity**
> half, which is our *launch-day experience for 100% of tenants*. The **scale-transition**
> half (pgvector→Qdrant migration, embedding drift at volume, per-tenant reindex) is
> explicitly **deferred** until we have volume — see §9.

---

## 1. The problem in one sentence

Every per-tenant statistical model gates behind a data threshold it takes weeks-to-months
of live traffic to cross, so at launch — and through every new customer's trial window —
the headline AI features (churn risk, QA/IRT scoring, calibrated confidences) are either
dark or running on an un-personalized fallback, and we have not decided, uniformly, what a
cold tenant should actually experience.

## 2. Why this is hard (not just unfinished)

It's the bias/variance problem wearing a product hat. Per-tenant models are *right* long-term
(each customer's calls, scorecards, and churn dynamics differ) but *impossible* short-term
(no tenant has data on day one). The hard part is the transition: serve something
reasonable-and-honest at n=0, and personalize *smoothly* as n grows — without a hard on/off
cliff at an arbitrary threshold, and without over-claiming confidence you haven't earned.
Pre-launch is the ideal — and only — time to design this before it's 100 tenants' day-one impression.

## 3. The cold-start gates today (evidence)

| Model | File | Gate | Behavior below gate | Warm-start today? |
|---|---|---|---|---|
| Churn (Cox PH) | `churn_model.py:41–42` | `MIN_TRAIN_EVENTS=150` / `RELIABLE=300` | `"insufficient_data"`, falls back to LLM inputs | ❌ trains per-tenant from zero |
| Scorecard IRT (2PL) | `irt.py:43` | `MIN_ITEM_RESPONSES=30` | item stays unfitted (`a=0,b=0`) | ❌ |
| Score calibration (Platt/ECE) | `calibration.py:43–44` | `MIN_CALIBRATION_SAMPLES=50`, activation blocked if `ECE>0.12` | uses **global default** (`tenant_id IS NULL`) | ✅ `active_calibration:372–398` |
| A/B winner (Wilson) | `campaign_winner_service.py:32` | `MIN_SENDS_PER_VARIANT=30` | no winner declared | n/a (needs real sends) |
| Paralinguistic baseline (z-score) | `paralinguistic_baseline.py:43` | `MIN_SPEAKER_UTTERANCES=4` | no outlier tags | per-speaker, resets each call |
| WER thresholds | `wer_service.py:29–30` | hardcoded 0.08 / 0.12 | global, not per-tenant/channel | n/a |

**The key asymmetry:** calibration already models the right cold-start shape — a global prior
new tenants inherit until they earn their own. Churn and IRT don't; they go dark. Whatever we
choose, the goal is *one consistent cold-start contract* across these models, not six ad-hoc ones.

## 4. The zero-tenant seeding problem

Calibration's global fallback only helps if a `tenant_id IS NULL` global row **exists**. At
zero tenants, nothing has created one. So any warm-start strategy has a bootstrap dependency:
where does the day-one global prior come from when there is no production data at all?
Candidate sources: `demo_seeder.py` seed data, synthetic generation, domain-informed hand
priors, or the founder's own real product-call data (the first real transcripts we can get).

## 5. The strategic fork (decide before building)

- **A — Lean on the LLM fallback for v1 (minimal).** Accept the statistical models stay dark
  at launch; invest only in making every model degrade *gracefully* to the LLM-driven path and
  render an honest "still learning" state. Cheapest, ships fastest. Defers the statistical
  value prop. Aligns with "don't add machinery before the data justifies it."
- **B — Warm-start via global prior + shrinkage (medium).** Generalize calibration's
  `tenant_id IS NULL` pattern so every per-tenant model serves a pooled cross-tenant baseline
  from day one and shrinks toward tenant-specific estimates as data accrues (empirical-Bayes /
  partial-pooling flavor). Every tenant gets a real model immediately; requires a seeded prior (§4).
- **C — Seed per-tenant from demo/synthetic data (enabling piece).** Bootstrap either the
  global prior (for B) or a per-tenant starter model from seed/synthetic/founder data.

These compose: B needs C to have anything to serve at n=0; A is the fallback B degrades to
per-model until each threshold is crossed.

## 6. Recommended first step

**Audit the cold-start path for each of the six models** (static, no traffic needed): for each,
what *exactly* happens at n=0 today, is the degradation graceful and honest in the UI, and is
the LLM/global fallback good enough to ship v1 on? That audit resolves the §5 fork with facts
instead of taste — it tells us whether we need B/C now or can defer to A and ship.

## 7. Open design questions

1. Which fork (A/B/C or a blend) is the v1 target?
2. If B: is generalizing calibration's `tenant_id IS NULL` global-default into a shared
   "warm-start" interface the right abstraction, or too clever?
3. If C: what seeds the day-one prior — demo data, synthetic, or real founder-product calls?
4. What is the *honest* confidence/UX contract at each stage (dark / learning / ok)? Today only
   churn and calibration express stages; the others are silently on/off.
5. Which model do we pilot the pattern on first — calibration (scaffolding exists), or churn
   (highest product value, currently worst cold-start)?

## 8. Chosen approach

_To be filled in once we've talked through §5–§7._

## 9. Deferred (scale-transition half — revisit at volume)

Not in scope pre-launch, tracked so we don't lose it: pgvector→Qdrant migration under load
(`vector_health_check.py`), per-tenant reindex with stale/fresh hybrid window, Voyage embedding
model-version drift with no version tag in vector metadata (`embedder.py`), and no delta-sync
in `kb/sync_runner.py`. These only bite once tenants and chunk counts are large.
