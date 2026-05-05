# Paralinguistic-Aware Analysis Pipeline — Plan

## Status
**Not started.** Planning doc only. Do not begin implementation until the user confirms.

## Goal
Give the AI textual-analysis stage access to paralinguistic (vocal) features in the same prompt, so extractions like sentiment, warnings, commitment confidence, and sarcasm detection can use acoustic signal — not just transcript text.

## Decision: Option A (Sequential, single LLM pass)

```
Ingestion → Transcription (with bundled Deepgram diarization) → Paralinguistics → AI Textual Analysis
```

### Why Option A over the alternatives

- **Keeps Deepgram's joint diarize+transcribe.** No separate diarizer to source, tune, or maintain. No risk of compounding upstream diarization errors into both the transcript and the paralinguistic per-speaker attribution.
- **Single LLM call gets both signals.** No two-pass orchestration; no speculative extraction.
- **Smallest refactor from today's pipeline.** Today, paralinguistics runs at step 17a (after AI analysis). The change is: move it earlier and feed its output into the analysis prompt.
- **Wall-clock cost is modest.** Paralinguistic extraction is ~5–15s for a typical call; the LLM call is the dominant latency anyway.

### Alternatives that were ruled out

- **Option B — Two-pass LLM (deep analysis on demand).** Better token economics, but adds orchestration complexity and a second LLM call on the calls that matter most. Reconsider only if token spend on deep extractions becomes a problem.
- **Option C — Split diarization, parallelize transcription ∥ paralinguistics.** Highest theoretical wall-clock win, but loses Deepgram's joint diarization quality, adds a parallel-branch merge step, and breaks triage-based gating. Reconsider only if production metrics show the serial paralinguistic step is a real bottleneck.

## Current state (reference)

- Transcription: [backend/app/tasks.py:1559-1642](../backend/app/tasks.py#L1559-L1642), [backend/app/services/transcription.py](../backend/app/services/transcription.py)
- AI textual analysis: [backend/app/tasks.py:707-829](../backend/app/tasks.py#L707-L829), [backend/app/services/ai_analysis.py:26-69](../backend/app/services/ai_analysis.py#L26-L69)
- Paralinguistics (today runs at step 17a, post-analysis): [backend/app/tasks.py:1187-1248](../backend/app/tasks.py#L1187-L1248), [backend/app/services/paralinguistics.py](../backend/app/services/paralinguistics.py)
- Tenant feature gates: `features_enabled.paralinguistic_analysis` (default on), `features_enabled.emotion_classification` (opt-in)

The paralinguistic extractor already takes diarized `SpeakerAudioSegment(speaker_id, start, end)` input and returns per-speaker + overall feature dicts. No change needed to the extractor itself for this refactor — only to where it's called from in the pipeline.

## Open design questions (resolve before implementation)

1. **Prompt representation.** How do paralinguistic features get injected into the AI-analysis prompt?
   - Inline tags interleaved with transcript turns: `[SpeakerA — pitch: high, pause before: 2.1s] "I'm fine."`
   - Structured side-block appended to the prompt: a per-speaker JSON summary plus a per-utterance timeline of notable acoustic events.
   - Hybrid: structured per-speaker summary + inline tags only on utterances that cross a "notable" threshold.
2. **Notability threshold.** Should we tag every utterance, or only those where acoustic features deviate meaningfully from the speaker's baseline (z-score, percentile, etc.)? Untagged utterances keep the prompt small.
3. **Graceful degradation.** When paralinguistics returns `available: false` (audio too short, parselmouth missing, extraction error), the analysis prompt must fall back cleanly to today's transcript-only behavior. No silent quality regression.
4. **Feature gating.** When the tenant flag `paralinguistic_analysis` is off, the pipeline should skip the extraction entirely and use the transcript-only prompt — no dead branch.
5. **Triage interaction.** Today, triage/complexity scoring runs at step 8 between transcription and AI analysis. Decide: does paralinguistic data get fed into triage too (potentially improving complexity scoring), or only into the downstream LLM call?
6. **Prompt-token budget.** Paralinguistic context costs tokens. Confirm the worst-case prompt size still fits under the model context limit and stays within reasonable cost per call.
7. **Backfill.** The recent admin backfill work (commit fc9a2da) re-ran warnings + commitments on already-analyzed interactions. Decide whether paralinguistic-enriched re-analysis should also be backfillable for historical calls where audio is retained.

## Scoring architecture roadmap (decided 2026-05-04)

The paralinguistic refactor lands inside a broader scoring-architecture shift. Decisions:

**Now — Option 3: LLM emits buckets, code maps to numbers.**
Drop calibrated decimals (`churn_risk: 0.85`, `sentiment_score: 7.2`, etc.) from the analysis prompt. Keep only categorical buckets (`'high' | 'medium' | 'low' | 'none'`). Map buckets to canonical numeric values in deterministic Python. Eliminates non-determinism and false precision before the paralinguistic refactor compounds the calibration problem.

**Soon — Option 1: LLM extracts evidence, deterministic rubric computes scores.**
LLM emits structured signals (pricing pushback, delay indicators, competitor mentions, positive signals, commitments, objections, quotes/timestamps). A scoring service maps evidence → numeric score via a transparent rubric. Decouples "what was true about the call" from "what number we show the user." Paralinguistic features become rubric inputs with explicit weights.

**Eventually — Option 2: small calibrated classifier on extracted features, trained on outcomes.**
Once outcome data exists, a logistic-regression / gradient-boosted classifier predicts churn / upsell / etc. from the extracted features. The LLM stays as the *feature extractor*, not the predictor. Cold-start tenants continue to use the Option 1 rubric.

**Throughout — Option 4 hybrid as the data-collection layer.**
Both LLM bucket and deterministic rubric scores logged side-by-side for every interaction so we can compare them, catch rubric blind spots, and eventually train Option 2 against actual outcomes.

### Important guardrails

- **"Calibration, not accuracy."** Churn isn't deterministic. Target: when we say 70%, 70% actually churn. Not "we got it right."
- **Don't fine-tune the LLM on outcomes.** LLM = extractor; small classifier = predictor. Different problems, different tools.
- **Sort each metric by type, don't ML everything.**
  - *Measurement* (compliance, action items, commitments mentioned) → Option 1, deterministic, never ML.
  - *Prediction* (churn risk, upsell score) → Option 1 first, then Option 2 once data justifies.
  - *Subjective summary* (call summary, coaching notes) → stays LLM, no scoring.
- **Outcome-data realities.** Selection bias (flagged customers get more follow-up), 6–18 month feedback loops on churn, ≥1k–10k labeled positives needed before Option 2 earns its keep. Plan accordingly.

## Phase 0 — Telemetry foundation (prerequisite, independent of paralinguistic refactor)

Must start logging *now* so that 12 months of paired data exists when Option 2 becomes viable. This work is unblocked and can run in parallel with everything else.

- Outcomes-joinable telemetry table keyed on `interaction_id` + tenant, capturing: LLM bucket output (each scored field), every extracted evidence signal, the deterministic rubric score (once Option 1 lands), prompt/model version, timestamp.
- Outcome-event ingestion: subscription cancellation, downgrade, no-renewal at term, expansion. Piped into the same store with the join keys. Stable customer identity across calls is required.
- Pre-declared label time horizons: 30 / 90 / 180 / 365 day churn, all stored — pick the right one later, don't lose options now.
- Selection-bias logging: capture which calls triggered rep follow-up, escalation, manager review, so we can model the intervention effect later.

## To-do (do not start until the user confirms)

### Phase 0 — Telemetry foundation (start anytime; unblocks everything else)
- [ ] Design schema for outcomes-joinable telemetry (LLM bucket, evidence signals, rubric score, prompt/model version)
- [ ] Wire telemetry write into AI-analysis pipeline so every interaction emits a record
- [ ] Define outcome events (cancel, downgrade, no-renewal, expansion) and ingest path; ensure stable customer identity across calls
- [ ] Store all candidate label horizons (30/90/180/365 day) rather than choosing one upfront
- [ ] Capture intervention signals (rep follow-up, escalation, manager review) for later bias correction

### Phase 1 — Option 3 (LLM buckets, deterministic numeric mapping)
- [ ] Strip calibrated decimals (`churn_risk`, `sentiment_score`, `upsell_score`, `script_adherence_score`) from the analysis system prompt
- [ ] Keep categorical buckets only in LLM output
- [ ] Add deterministic bucket→number mapping in code; expose both bucket and number to downstream consumers
- [ ] Update any UI / API surfaces that consumed the LLM decimals to consume the deterministic mapping instead
- [ ] Migration plan for historical scores (recompute from stored buckets where possible; flag legacy scores otherwise)

### Phase 2 — Paralinguistic-aware analysis (this doc's original scope)
- [ ] Resolve seven open design questions with user (prompt representation, notability threshold, fallback, gating, triage, token budget, backfill)
- [ ] Decide prompt representation (inline tags vs side-block vs hybrid) and write template
- [ ] Move paralinguistic extraction earlier in [backend/app/tasks.py](../backend/app/tasks.py) — before AI analysis, after transcription
- [ ] Wire paralinguistic features into AI-analysis prompt in [backend/app/services/ai_analysis.py](../backend/app/services/ai_analysis.py)
- [ ] Add fallback path when paralinguistics unavailable or feature-gated off
- [ ] Confirm triage-stage handling — feed acoustic signal in or not
- [ ] Observability: per-stage timing, paralinguistic availability rate, token-cost delta
- [ ] Tests: unit tests on prompt construction with/without paralinguistic context; integration test on full pipeline
- [ ] Backfill task (if in scope): re-run analysis with paralinguistic enrichment on historical interactions
- [ ] Verify both apps (backend `linda-staging` AND SPA `linda-staging-app`) deploy cleanly on merge

### Phase 3 — Option 1 (evidence schema + deterministic rubric)
- [ ] Define structured evidence schema (pricing pushback, delay indicators, competitor mentions, commitments, objections, quotes/timestamps, paralinguistic deltas, etc.)
- [ ] Update analysis system prompt to emit evidence (replacing inline scoring)
- [ ] Build scoring service: deterministic rubric mapping evidence → score, with explicit weights for paralinguistic inputs
- [ ] Sort each existing scored field into measurement / prediction / subjective and route accordingly
- [ ] Compliance / script adherence move to fully deterministic (extraction-only LLM step)
- [ ] Continue dual-logging LLM bucket + rubric score (Option 4 hybrid) for divergence monitoring

### Phase 4 — Option 2 (small classifier on outcomes, when data justifies)
- [ ] Gate on volume: ≥1k–10k labeled positive outcomes before starting
- [ ] Pick label horizon (likely 90 or 180 day churn first)
- [ ] Train small calibrated classifier (logistic regression / GBT) on extracted features
- [ ] Calibration evaluation (reliability diagrams, Brier score), not just accuracy
- [ ] Cold-start fallback: new tenants / verticals stay on Option 1 rubric until they have enough data

## Phase 5 — Presentation & Action Layer

### Goal

Convert every signal the pipeline produces — currently displayed, hidden, planned, or missing — into a usable, understandable, visually appealing surface that drives action. Replace per-call clutter with a clean tab structure. Demote internal-only diagnostics to background. Push every actionable signal toward a one-click next step.

### Surfaces (where things live)

**Interaction page (single call)** — four tabs:
- *Overview* — summary (third person), mood badge, Call Dynamics timeline, action items, top-of-page compliance and missing-next-step warnings (only when triggered)
- *Transcript* — inline-tagged transcript with mouseover popups; topic chips
- *Coaching* — overall coaching points, talk/listen split, Rapport gauge, methodology scorecard
- *Compliance* — script adherence checklist, gap details (only listed if any)

**Customer page** — four tabs:
- *Signals* — churn risk, upsell, Customer Behavior Radar, Change-Readiness Index
- *Interactions* — calls for this customer; full sort/filter; full per-call drill-down
- *Action items* — across all calls with this customer
- *Notes / files / contacts*

**Global Interactions page** — all calls across all customers; identical drill-down depth and capabilities to the customer-page Interactions tab; same sort/filter set.

**Manager surfaces:**
- Aggregate team dashboard (talk/listen distribution, methodology adherence trends, churn-flag throughput)
- Product feedback library (themed, searchable; weekly digest)
- Training-gap dashboard: per-rep reflection rate, open/closed question rate, methodology adherence
- Scorecard review queue (research-gated — see below)

**Admin surfaces:**
- Training corpora (best/worst examples; merged-key-moments stream)
- Outcome / model performance (calibration plots, rubric-vs-LLM divergence, drift)
- Rep diagnostic deep-dive
- Scorecard review queue (admin visibility)

**Background-only (no UI, feeds other outputs):**
- Customer Effort markers → churn reasoning, manager meta-analysis
- MI mechanics (change talk, sustain talk, commitment language) → Behavior Radar, Change-Readiness Index, coaching points
- Per-call reflection rate, per-call open/closed rate → manager aggregates only
- Vocal stress markers → surfaced only as inline "tense moment" tags
- Linguistic Style Match, vocal accommodation → Rapport gauge

### Inline tagging system (Transcript tab)

One visual language: colored highlight + matching text color, distinguished by color only. Mouseover popup = type + one-line context + "create action item" button. Unlimited highlights per call. Color palette:

| Tag | Color |
|---|---|
| What went well | Soft green |
| What to improve | Soft amber |
| Competitor mention | Light blue |
| Customer commitment | Light gold |
| Resolved objection | Light teal |
| Unresolved objection | Soft red |
| Tense moment | Pale yellow |
| Low-confidence transcription | Pale gray |

Color-blind-safe palette required; verify before lockdown.

### Visualizations

**Call Dynamics chart** (Overview tab) — single timeline with three toggleable layers: customer mood (sentiment line), customer vocal energy (ribbon), rep talk-density (bars). Click anywhere → jumps to that second of audio + transcript. Replaces three previously separate charts.

Timeline pin clutter management (this is the discrete-marker layer, not inline highlights): cluster pins within ~20s of each other, default-show top 6 by severity, expand on zoom, type filters (objection / commitment / question / risk / win).

**Topic chips** (Transcript tab) — sized circles by mention count. Mouseover highlights every instance of that topic in the transcript. Click opens a side panel with related KB articles, related/prior calls, and a "create custom action item linked to this topic" button. Marked experimental — remove if not earning placement.

**Rapport gauge** (Coaching tab) — 3-bucket (strong / building / weak). UI label stays "Rapport." Internal composition: Linguistic Style Match + vocal accommodation + reflection rate + low interruption rate + positive affect markers. Contributing signals visible on hover.

**Methodology scorecard** (Coaching tab) — 4-quadrant card per the rep's playbook (SPIN for sales, structured-resolution for CS, per-tenant config). Missing quadrant flagged with a suggested question for the next call.

**Customer Behavior Radar** (Customer Signals tab) — 6 axes, all derivable from existing extractions:
1. Commitment (forward-language frequency)
2. Openness (change talk vs sustain talk)
3. Engagement (vocal energy + question rate)
4. Trust (context-sharing + agreement with reframes)
5. Decision urgency (timeline language)
6. Friction (unresolved objection density, inverted)

**Change-Readiness Index** (Customer Signals tab) — single 0–100 score derived from the radar axes, weighted by outcomes the system observes. Rule-based at launch; weighting tightens as Phase 0 outcome data accrues. Same surface label, increasing predictive value over time.

**Churn risk + Upsell** (Customer Signals tab, paired in one block) — traffic-light + 2–3 plain-language reasons + 1 plain-language suggested play. No technical jargon in user-facing text.

### Action item lifecycle

- Each action item owns its own draft email (subject, body, recipients) and optional call-script bullets
- States: pending / completed / snoozed (with wake date) / dismissed (with reason)
- Replaces the existing four-variation email system at the bottom of customer pages — retired in the same change, not gradually
- Dismiss reasons feed a learning loop: consistent dismiss patterns suppress future similar suggestions in matching contexts (e.g. if reps consistently dismiss "schedule technical review" on sub-$10k deals, stop suggesting it there)

### Summary tone

Drop the first-person "Linda's voice" framing in the analysis system prompt. Neutral third person across all LLM analysis outputs ("The customer pushed back on pricing. The rep responded by..."). Affects summary, key moments, coaching context, and notable snippet descriptions. Product chat agent ("Linda") keeps its conversational voice — this only affects analysis output prose.

### Disposition table — what's IN / OUT / BACKGROUND

**IN (agent-visible):** Call Dynamics chart, action items with embedded emails, inline tag highlighting, topic chips (experimental), Rapport gauge, methodology scorecard, Customer Behavior Radar, Change-Readiness Index, paired churn/upsell, key moments (notable snippets merged in), compliance warnings.

**IN (manager only):** aggregate team dashboard, product feedback library, reflection rate per rep, open/closed question rate per rep, methodology adherence trends, scorecard review queue.

**IN (admin only):** training corpora, outcome/model performance, rep diagnostic deep-dive, scorecard review queue.

**OUT entirely:** psychological safety; standalone follow-up email section (folded into action items); standalone notable snippets surface (merged into key moments).

**BACKGROUND (no UI):** customer effort markers, MI mechanics, per-call reflection rate, per-call open/closed rate, vocal stress markers, Linguistic Style Match, vocal accommodation.

### Research dependency: scorecard-driven coaching

Scorecard review surfaces (manager + admin) need a research foundation before implementation. Building the UI without the framework risks shipping a feature that does more harm than good — research consistently shows that poorly-framed performance feedback can demotivate skilled workers and damage retention.

**Research scope:**
- Survey instructional coaching, formative assessment, motivational interviewing for management contexts, and performance management literature
- Identify what actually drives behavior change in skilled-work contexts (vs. what merely documents performance)
- Define review queue triage rules: which negative scorecards warrant manager intervention, which are noise, which need to escalate to admin
- Establish guardrails against feedback patterns the literature shows backfire (overly numeric performance review, threat-based language, cherry-picked negative examples without context, public shaming dynamics)
- Produce a manager-executable coaching framework: how to convert scorecard signals into specific, actionable, growth-oriented coaching plans

**Output:** A framework document that drives both the review queue triage logic and the coaching-prompt language the system suggests to managers. This is a prerequisite to building the review surface.

### Phase 5 to-do (do not start until user confirms)

#### Tab structure & navigation
- [ ] Build interaction page with four tabs (Overview, Transcript, Coaching, Compliance)
- [ ] Build customer page with four tabs (Signals, Interactions, Action items, Notes)
- [ ] Build Global Interactions page; per-call drill-down identical to customer-page Interactions tab
- [ ] Sort/filter set on both interaction surfaces: date, score, churn risk, methodology, rep, length, unresolved action items, mood, has compliance gap

#### Inline tagging & chips
- [ ] Inline transcript tag system (color-only visual language, mouseover popup, action item link)
- [ ] Color-blind-safe palette verification before lockdown
- [ ] Topic chip system (sized circles, mouseover highlight, click → side panel)
- [ ] Chip side panel: KB articles, related/prior calls, custom action item linker
- [ ] Mark chips experimental; instrument usage; remove if not earning placement

#### Visualizations
- [ ] Call Dynamics unified chart (mood + energy + talk-density, toggleable layers, click-to-jump)
- [ ] Timeline pin clutter management (cluster, severity-weight, type filters)
- [ ] Rapport gauge with composite-signal hover detail
- [ ] Methodology scorecard (SPIN for sales, structured-resolution for CS, per-tenant config)
- [ ] Customer Behavior Radar (6 axes from existing extractions)
- [ ] Change-Readiness Index (rule-based first; outcome-calibrated weighting once Phase 0 data accrues)
- [ ] Paired churn/upsell Signals block with plain-language reasons + plays

#### Action item rebuild
- [ ] Schema: each action item owns draft email + call-script bullets + state
- [ ] States: pending / completed / snoozed / dismissed (with reason)
- [ ] Retire 4-variation email system at the bottom of customer pages
- [ ] Dismiss-reason learning loop wiring
- [ ] UI: stacked cards on Overview tab + customer-level Action items tab

#### Prompt + extraction changes
- [ ] Drop "Linda's voice" first-person framing from analysis prompt; neutral third person
- [ ] Emit inline tag annotations (per-utterance type + brief context) from LLM extraction
- [ ] Emit objection resolution status (resolved / unresolved) per objection
- [ ] Surface commitment language detection as inline tags (already extracted)

#### Manager + admin surfaces
- [ ] Move product feedback library off agent surfaces; build manager view
- [ ] Manager aggregate dashboard (talk/listen, methodology trends, churn throughput)
- [ ] Manager training-gap dashboard (reflection rate, open/closed rate, methodology adherence per rep)
- [ ] Demote items to background: customer effort, MI mechanics, per-call reflection/question-type counts

#### Scorecard reviews (research-gated)
- [ ] **RESEARCH:** survey coaching/feedback literature; identify what drives behavior change in skilled-work contexts
- [ ] **RESEARCH:** define review queue triage rules and guardrails against demotivating feedback patterns
- [ ] **RESEARCH:** produce a manager-executable coaching framework document
- [ ] Build scorecard review queue surface (manager + admin)
- [ ] Wire scorecards to review queue with research-derived triage

#### Cross-cutting
- [ ] Reinforcement parity check: every "what could be better" surface has a corresponding "what went well" surface
- [ ] Action button audit: every actionable signal has a one-click next step (send / add to CRM / schedule / mark for review / dismiss)
- [ ] Usability testing before locking the tab structure
