# Scoring & Orchestration Architecture

**Status:** design-of-record for the metrics, scoring, and orchestrator layer.
Versioned alongside the code.  Prior iteration: all statistics were recomputed
ad-hoc from `interactions.insights` JSONB with a single LLM producing both
categorical and numeric outputs.  That approach was uncalibrated, confused
presentation with computation, and duplicated work across endpoints.

This document describes the new architecture.  Every design choice below is
driven by three non-negotiable constraints from the product owner:

1. **Cost and speed first.**  Favor deterministic math over LLM calls,
   smallest viable model over larger, batched APIs over real-time, and
   cached prompts / embeddings over recomputation.
2. **Proprietary aggregation, user-facing simplicity.**  Every signal in
   the feature store feeds scoring; only the top-K most significant
   factors surface to end users.  Full weight vectors, raw feature
   importance, and calibration parameters stay server-side.
3. **Measurable quality.**  Every scorer emits a confidence; drift is
   monitored continuously; corrections (not cold-start labels) grow the
   evaluation set.

---

## 1. High-level topology

```
                          ┌──────────────────────────────┐
                          │       BusinessProfile        │  ← Opus 4.6, weekly
                          │   (tenant-level rollup)      │
                          └──────────────┬───────────────┘
                                         │
         ┌───────────────┬───────────────┼───────────────┬───────────────┐
         │               │               │               │               │
┌────────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
│ ManagerProfile│ │AgentProfile│ │AgentProfile │ │ClientProfile│ │ClientProfile│  ← Opus, daily
│  (per manager)│ │  (per agent)│ │             │ │             │ │             │
└────────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
         │               │               │               │               │
         └───────────────┴──────┬────────┴───────────────┴───────────────┘
                                │
                        ┌───────▼────────┐
                        │ DeltaReport[]  │  ← Sonnet, real-time (≤300 tok)
                        │ (per interaction)
                        └───────┬────────┘
                                │
                 ┌──────────────▼──────────────┐
                 │     InteractionFeatures     │  ← deterministic + Sonnet/Haiku
                 │   canonical per-call store   │
                 └──────────────┬──────────────┘
                                │
                 ┌──────────────▼──────────────┐
                 │   Raw interaction (audio/txt)│
                 └─────────────────────────────┘
```

Feature data flows upward; orchestrator directives and calibrated weights
flow downward.

---

## 2. Data layer

### 2.1 `InteractionFeatures` (new table)

One row per `interaction`.  Canonical source for every metric.  Never
re-derive from `interactions.insights`.

Columns:

| Column | Type | Source |
|---|---|---|
| `interaction_id` (PK, FK) | UUID | — |
| `tenant_id` (FK) | UUID | — |
| `deterministic` | JSONB | computed from transcript timestamps + word lists |
| `llm_structured` | JSONB | parsed Sonnet/Haiku analysis output |
| `embeddings_ref` | TEXT | S3/Qdrant pointer, not inline |
| `proxy_outcomes` | JSONB | reply, renew, churn, action-item-closed events (filled async) |
| `scorer_versions` | JSONB | {sentiment_model: "v3", churn_model: "v1", …} |
| `created_at` / `updated_at` | timestamptz | |

**`deterministic` schema** (all computed from diarized transcript):

```json
{
  "talk_pct": {"agent": 0.42, "customer": 0.55, "silence": 0.03},
  "longest_monologue_sec": {"agent": 72.1, "customer": 31.4},
  "longest_customer_story_sec": 31.4,
  "interactivity_per_min": 5.4,
  "patience_sec": 0.74,
  "turn_entropy": 0.92,
  "back_channel_rate_per_min": {"agent": 7.1, "customer": 3.2},
  "filler_rate_per_min": {"agent": 4.2, "customer": 6.8},
  "question_rate_per_min": 2.3,
  "words_per_min": {"agent": 142.5, "customer": 118.9},
  "interruption_count": {"cooperative": 3, "intrusive": 1},
  "pause_distribution_sec": {"p50": 0.52, "p90": 1.9, "max": 6.3},
  "laughter_events": 2,
  "linguistic_style_match": 0.81,
  "pronoun_ratio_we_i": {"agent": 1.4, "customer": 0.6},
  "sentiment_trajectory_turns": [[0, 0.3], [1, 0.5], ...],
  "stakeholder_count": 4
}
```

**`llm_structured` schema** (LLM-sourced, one call per interaction via
tiered router):

```json
{
  "summary": "...",
  "sentiment_overall": "positive",
  "sentiment_score_llm": 7.5,
  "topics": [{"name": "pricing", "canonical_name": "pricing", "relevance": 0.8, "anchors": ["§3", "§7"]}],
  "key_moments": [...],
  "competitor_mentions": [...],
  "product_feedback": [...],
  "action_items": [...],
  "coaching_reflections": [...],
  "commitment_language_count": 3,
  "change_talk_count": 7,
  "sustain_talk_count": 2,
  "reflections_by_agent_count": 4,
  "objections_raised": [...],
  "objections_resolved": [...],
  "next_step_specific": true,
  "next_step_structure": {"date": "2026-05-01", "attendees": [...]}
}
```

### 2.2 Four profile tables (new)

Each profile is versioned (append-only) with an audit trail of the event
that triggered each update.  Shape:

| Column | Type | Purpose |
|---|---|---|
| `id` (PK) | UUID | |
| `entity_id` (FK) | UUID | contact_id / user_id / user_id / tenant_id |
| `tenant_id` (FK) | UUID | |
| `version` | int | monotonic per entity |
| `profile` | JSONB | full profile payload (schema below) |
| `top_factors` | JSONB | top-K factor list served to clients |
| `source_event` | JSONB | what caused this version (delta_report_id / weekly_reflection) |
| `confidence` | float | 0–1, orchestrator's self-assessed confidence |
| `created_at` | timestamptz | |

Tables: `client_profiles`, `agent_profiles`, `manager_profiles`,
`business_profiles`.  Latest version per entity is identified by
`(entity_id, MAX(version))`; a materialized view
`*_profiles_current` keeps reads cheap.

### 2.3 `DeltaReport` (new table)

Queue of per-interaction structured reports produced by Sonnet immediately
after an interaction's feature row lands.  Consumed by the daily
orchestrator pass.  Fields: `interaction_id`, `entity_scopes`
(array of `{entity_type, entity_id}`), `delta_json`, `consumed_at`.

---

## 3. Scoring framework

### 3.1 Score contract

Every scorer implements:

```python
def score(features: InteractionFeatures) -> ScoreResult: ...
```

where

```python
@dataclass
class ScoreResult:
    value: float            # 0–100 or 0–1 depending on the score
    confidence: float       # 0–1
    top_factors: list[Factor]  # 3–5 most influential, signed and labeled
    recommendations: list[Recommendation]  # 1–3 prioritized actions
    scorer_version: str
    calibrated: bool        # was Platt/isotonic calibration applied?
```

`Factor` captures enough to render a UI chip: `{label, direction ("+"|"-"),
magnitude_pct, why}`.  `Recommendation` is `{action, priority,
expected_impact, basis_factor_ids}`.

### 3.2 Factor importance decomposition

For any linear or additive composite (`health`, `agent_performance`,
`deal_momentum`):

1. Standardize each feature `z_j = (x_j − μ_j) / σ_j` against the
   tenant-level rolling baseline.
2. Compute `contribution_j = β_j · z_j`.
3. Rank by `|contribution_j|`; emit top-K.
4. Human-readable `why` string is pulled from a registry keyed on
   feature name + sign.

For ML-model scores (Cox hazard, calibrated GBM), use SHAP values from
`shap.TreeExplainer` when available, else fall back to the standardized
coefficient approach on the model's linear surrogate.

### 3.3 Calibration

Raw scorer outputs (LLM numerics, ML logits) are wrapped in
`CalibratedScorer` which applies Platt or isotonic mapping fit on a
held-out set of `(raw_score, proxy_outcome)` pairs.  Proxy outcomes:

| Score | Proxy outcome (observable) |
|---|---|
| Sentiment | customer replied positively within 48h / escalated |
| Churn risk | customer cancelled within 30/60/90d |
| Health score | renewed / upgraded at term |
| Action-item quality | action item closed in CRM within 14d |
| Coaching impact | agent's close rate delta over next 30d |

Calibration models are versioned and retrained weekly by the orchestrator.

### 3.4 Presentation layer contract

User-facing endpoints return the *outer* layer only.  The full feature
vector, raw β coefficients, and calibration parameters are not exposed
via API.  The `/analytics/*` responses include `top_factors` and
`recommendations` arrays capped at the tenant's configured `max_factors`
(default 3).

Expert mode (tenant setting `expert_mode_enabled`) can raise the cap,
but the raw model weights remain server-only.

---

## 4. Model routing and cost controls

### 4.1 Tier definitions

| Tier | Model | Typical use | Median cost per call |
|---|---|---|---|
| `haiku` | `claude-haiku-4-5-20251001` | Triage, single-field classification, summary-of-summary | $ |
| `sonnet` | `claude-sonnet-4-6` | Full structured analysis, delta report writing, scorecard | $$ |
| `opus` | `claude-opus-4-7` | Orchestrator consolidation, weekly reflection, quality review | $$$ |

### 4.2 Router decision logic

Inputs: `{task_type, complexity_score, transcript_len, tenant_tier,
retry_count}`.  Rules (in order):

1. If `task_type` is `orchestrator_*` → `opus`, always.
2. If `task_type` is `delta_report` → `sonnet` (fixed, small output).
3. If `task_type` is `main_analysis`:
    - `complexity_score < 0.35` → `haiku`.
    - `0.35 ≤ complexity < 0.75` → `sonnet`.
    - `≥ 0.75` OR `transcript_len > 12000 tokens` → `sonnet` with
      extended thinking budget if configured.
4. `tenant_tier == "enterprise"` bumps one tier up on borderline.
5. `retry_count > 0` bumps one tier up.

All routes go through `ModelRouter.invoke(task, *, prefer_batch=False)`
which also applies the cache/batch decisions below.

### 4.3 Prompt caching

Every LLM call uses Anthropic prompt caching via `cache_control:
ephemeral` on:

- The task's system prompt (already done today).
- **New:** tenant-scoped context block — canonical topic vocabulary,
  active scorecard templates, the tenant's current glossary/style guide.
- **New:** agent profile header (short bio + recent weak skills) when
  an agent-specific prompt is rendered.
- **New:** client profile header (relationship summary + last 3 deltas)
  when a client-specific prompt is rendered.

These headers are precomputed once per (tenant/agent/client) and only
re-rendered on profile version bump.

### 4.4 Batch API

Any non-user-facing job uses the Anthropic Messages Batches API for a
~50% token discount and no rate-limit pressure:

- Nightly tenant-insights rollup (already wired).
- Weekly orchestrator reflection pass.
- Backfill over `insights` JSONB.
- Drift/quality re-scoring on the golden set.

Live-path jobs (within 5 minutes of a call) remain on the sync API.

### 4.5 Embedding cache

Sentence embeddings (384-d minilm or 1536-d text-embedding-3-small) are
computed once per utterance and stored keyed on `sha256(text)`.  Reused by:

- Topic clustering (BERTopic).
- LSM similarity computation.
- Semantic search / RAG.
- Active-learning uncertainty (distance-to-centroid).

Cache backend: Redis hot tier (24 h TTL) + S3 cold tier for historical.

---

## 5. The Orchestrator

### 5.1 Responsibilities

The orchestrator is the single Opus-powered service that maintains the
four profile trees and their associated recommendations.  Its contract:

> Given all `InteractionFeatures`, `DeltaReports`, and observed proxy
> outcomes for a tenant since last run, produce an updated
> `BusinessProfile`, updated `ManagerProfile` per manager, updated
> `AgentProfile` per agent, and updated `ClientProfile` per contact.
> Each update carries a confidence, a short rationale, and a top-K
> factor list derived from measurable features, not narrative.

### 5.2 Three-cadence update loop

| Cadence | Runs | Model | What happens |
|---|---|---|---|
| Real-time | On `interaction.status = "analyzed"` event | Sonnet | Produce one `DeltaReport` per entity the call touches. ≤300 tokens structured JSON. |
| Daily | `0 4 * * *` UTC | Opus via batch API | Consolidate the day's deltas into per-entity profile updates. Bump version. |
| Weekly | `0 5 * * 1` UTC | Opus via batch API | Deep reflection: compare predicted outcomes to observed outcomes, rewrite weights, regenerate canonical vocabularies, surface drift. |

### 5.3 DeltaReport schema

Small, typed, side-effect-free.  Example:

```json
{
  "interaction_id": "...",
  "scopes": [
    {"type": "client", "id": "<contact>"},
    {"type": "agent", "id": "<user>"},
    {"type": "manager", "id": "<user>"},
    {"type": "business", "id": "<tenant>"}
  ],
  "client_delta": {
    "sentiment_shift": -0.8,
    "new_objections": ["pricing vs competitor X"],
    "resolved_objections": [],
    "champion_health_change": -0.2,
    "buying_signals_detected": [],
    "churn_risk_factors_added": ["mentioned budget freeze"]
  },
  "agent_delta": {
    "strengths_observed": ["structured discovery"],
    "gaps_observed": ["missed CFO stakeholder invitation"],
    "skill_exercised": ["objection_handling"],
    "metric_snapshots": {"LSM": 0.78, "reflection_ratio": 1.1}
  },
  "manager_delta": {
    "escalations_to_flag": [],
    "coaching_priority_hint": "objection_handling for <agent>"
  },
  "business_delta": {
    "trend_evidence": {"topic": "pricing", "direction": "up"},
    "content_gaps": ["no ROI deck for SMB segment"],
    "competitive_threat": "Competitor X pricing noted again"
  }
}
```

### 5.4 Profile schemas

All profiles share an outer skeleton:

```json
{
  "as_of": "2026-04-17T04:00:00Z",
  "version": 42,
  "summary": "Opus-generated 2–3 sentence narrative",
  "metrics": {...},
  "top_factors": [Factor, ...],
  "recommendations": [Recommendation, ...],
  "history": [{"version": 41, "as_of": "...", "headline": "..."}, ...]
}
```

**`ClientProfile.metrics`** includes: sentiment trajectory, rolling
churn-risk percentile, engagement breadth (distinct stakeholders),
champion health, outstanding objections, resolved objections,
commitment-language density, buying-signal count, days-to-next-step,
preferred communication style (LSM-derived), product-feedback themes,
NPS/CSAT proxy, time-in-stage vs. median, competitor-pressure index.

**`AgentProfile.metrics`** includes: win-rate by call type and stage,
complexity-adjusted win rate, LSM with customers (p50, p90),
reflection-to-question ratio, script adherence checklist coverage,
objection-resolved rate, talk-listen ratio histogram, patience p50,
question-rate by stage, action-item-close rate, skill-specific scores
(discovery/objection/closing) with EWMA, coaching completion rate,
rampedness index for new hires.

**`ManagerProfile.metrics`** includes: team size and composition by
tenure, roll-up of agent-profile metrics weighted by volume,
at-risk-account count and trend, coaching debt (open coaching actions),
escalation queue, cross-agent patterns (e.g., "3 agents missed the
same disclosure"), pipeline coverage ratio, deal-mix concentration.

**`BusinessProfile.metrics`** includes: tenant health index (composite),
cohort curves of deal quality by origination quarter, PSI of feature
distributions vs. baseline, topic-trend report (log-odds significant
risers and fallers), competitive-pressure index, product-feedback theme
clusters, pricing-sensitivity index, content gaps, team-wide skill gaps,
agent hiring signal (capacity vs. volume trend), ARR-at-risk estimate,
ARR-expansion-opportunity estimate.

### 5.5 Self-improvement loop

Weekly reflection compares predicted vs. observed outcomes on every
calibrated score.  If calibration ECE degrades beyond a threshold,
Opus rewrites the β weights and schedules a recalibration batch job.
Version bumps on scorer weights are logged and associated with the
profile updates they produced, so a bad rewrite can be rolled back.

---

## 6. Data quality and observability

### 6.1 Continuous quality metrics

Computed nightly and exposed on `/analytics/quality` (admin-only):

- **Krippendorff's α** between two Sonnet runs on 1% of calls.
- **Test-retest correlation** on a rotating golden set.
- **Calibration ECE** for each calibrated scorer against its proxy.
- **Population Stability Index** for the feature distribution vs. the
  90-day baseline.
- **Snorkel LF coverage and conflict** for any weak-supervision
  classifiers.

Thresholds that trigger paging: α < 0.7, ECE > 0.12, PSI > 0.25.

### 6.2 Active-learning correction queue

The UI surfaces items where the ensemble disagreement or the
calibrated-probability uncertainty is highest.  Each correction writes
to `correction_events` and appends to the golden set.  Corrections are
never required; the system works without any, they just improve it.

### 6.3 Audit

Every profile version, every scorer version, and every calibration
refit is traceable to the events that produced it via
`source_event` / `scorer_versions`.

---

## 7. Implementation phases

**Phase A (this PR):**
- Architecture doc (this file).
- Migration adding `interaction_features`, `delta_reports`,
  `client_profiles`, `agent_profiles`, `manager_profiles`,
  `business_profiles`.
- Deterministic-extractor expansion with tests.
- Statistics library.
- Score engine framework with factor decomposition.
- Model router + prompt cache helpers + batch API wrapper.
- Orchestrator service skeleton with three cadences wired to Celery
  Beat.
- Pipeline refactor: features → feature store → delta report enqueue.
- Presentation endpoints returning `{score, confidence, top_factors,
  recommendations}`.

**Phase B (next PR):**
- BERTopic pipeline on stored embeddings.
- Snorkel weak-supervision labelers for cancel-intent, commitment,
  objection resolution.
- Proxy-outcome ingestion from CRM integrations.
- Platt/isotonic calibration jobs.
- Active-learning correction UX on the web app.

**Phase C:**
- Cox survival model for churn, trained on observed cancellations.
- Mixed-effects leaderboards.
- IRT rubric calibration with accumulated scorecard history.
- Vintage cohort + PSI dashboards.
- Paralinguistic features from retained audio.
