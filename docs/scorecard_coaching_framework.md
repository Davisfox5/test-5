# Scorecard-Driven Coaching — Research Framework

This document covers the three RESEARCH items from Phase 5:
1. What drives behavior change in skilled-work contexts
2. Review queue triage rules and guardrails
3. Manager-executable coaching framework

It produces the operational rules for the scorecard review queue and the coaching-prompt language the system suggests to managers.

> **Status — first draft.** This is a working framework grounded in established literature, not a final product. Validate with: actual coaching practitioners, a tenant pilot before broad release, and ongoing review against the outcome data Phase 0 collects.

## 1 · What drives behavior change in skilled-work contexts

The coaching literature converges on a small number of high-leverage findings. Five hold up best for our context (sales reps and customer-service agents):

**Finding 1 — Specific, actionable feedback beats generalized evaluation.**
Hattie & Timperley (2007, *The Power of Feedback*) found that feedback at the *task* and *process* levels (what was done, how it was done) produces large effect sizes, while feedback at the *self* level ("you're a great rep" / "you're underperforming") produces no effect or harms motivation. Translation for us: the system should suggest "ask one open-ended discovery question before the demo next time" — not "improve your discovery skills."

**Finding 2 — Reinforcement of what's working is at least as important as correction.**
Aguilar's *Art of Coaching* and the broader instructional-coaching literature find that coaching conversations that lead with what's working — and ground correction in the rep's own observed strengths — produce sustained behavior change. Coaching that leads with deficiencies produces compliance during observation and regression after. This is why Phase 5 has a **reinforcement parity** todo: every "what could be better" surface needs a paired "what went well" surface.

**Finding 3 — Self-discovery beats prescription.**
Motivational interviewing research (Miller & Rollnick) and adult-learning theory (Knowles) both show: behavior change is more durable when the learner identifies the gap themselves, with guidance, than when it is prescribed to them. Translation: the manager UI should support a "show the rep this evidence and ask what they notice" flow — not a "tell the rep what they did wrong" flow.

**Finding 4 — Frequency and proximity matter more than magnitude.**
Small, regular, in-context coaching (within hours of the call) produces more change than monthly performance reviews. Translation: the review queue should surface flagged calls *fast*, the coaching artifact should be lightweight (15-30 min conversations, not 90-min reviews), and the system should track whether feedback gets delivered in days or weeks.

**Finding 5 — Trust between coach and learner is the largest single predictor of coaching effectiveness.**
Multiple meta-analyses (Theeboom et al. 2014; Jones et al. 2016) find that the working alliance between coach and coachee accounts for more variance in outcomes than the specific coaching technique used. Translation: the system should *not* generate coaching content the manager delivers verbatim; it should surface evidence the manager can interpret and discuss in their own voice. Algorithmically-generated feedback delivered without manager filtering damages the relationship and underperforms.

### What this rules out

- Numeric performance reports as a primary coaching artifact ("you scored 67")
- "Bottom of the leaderboard" public ranking as a coaching mechanism
- Threat-based language ("you must improve discovery in 30 days or...")
- Cherry-picked failure clips delivered without the rep present and without context
- Delivering AI-generated coaching scripts verbatim to reps

### What this favors

- Evidence-grounded conversation prompts ("Here's a moment from yesterday's call. What do you notice?")
- Paired-evidence framing ("Here's what worked here, and here's where it ran out of room")
- Time-boxed micro-coaching (15-30 min, on a single observed pattern)
- Manager autonomy to interpret and frame the evidence in their own words

## 2 · Review queue triage rules

The review queue surfaces calls that warrant manager attention. Volume matters: too many false positives and managers ignore the queue; too few and real problems slip through. Triage rules.

### Always-surface (high signal):
- Compliance gap with severity = high (regulatory or contractual)
- Customer escalation language present + unresolved
- Unresolved objection in a top-tier deal (deal value or strategic flag)
- Churn risk = high with no follow-up action item created within 24 hours

### Surface when sustained over a window (pattern signal):
- Reflection rate < 30% of team median over 5 consecutive calls
- Open-question rate < 25% of team median over 5 consecutive calls
- Talk-listen ratio > 65% rep over 5 consecutive calls
- Methodology coverage missing the same quadrant (e.g. always missing "Implication" in SPIN) over 3+ calls

### Suppress (would generate noise):
- Single calls where rep performance dips but their week-over-week trend is stable
- Calls flagged for compliance gaps that have already been remediated
- Calls where the customer-side issue dominates and the rep handled it appropriately
- Newly-onboarded reps in their first 14 days (use a separate onboarding-coaching surface, not the review queue)

### Triage labels surfaced to manager:
- `Coach` — pattern over time, growth-oriented conversation suggested
- `Address` — single high-stakes incident requiring direct discussion
- `Escalate` — compliance / regulatory / customer-escalation; loop in admin
- `Reinforce` — positive pattern worth recognizing (the queue surfaces wins, not just gaps)

The `Reinforce` label is critical. A queue that only surfaces problems trains managers to associate the queue with negativity and they will avoid it. Roughly one-third of queue items should be `Reinforce` to keep manager engagement and satisfy the reinforcement-parity finding above.

### Guardrails

The system MUST NOT:
- Auto-generate disciplinary documentation from queue items
- Surface queue items to anyone except the rep's direct manager and admins (no peer visibility)
- Calculate "scores" the manager is expected to defend numerically to the rep ("the system says your discovery score is 58")
- Re-surface the same call after the manager has marked it reviewed, except when new outcome data materially changes the assessment
- Suggest specific coaching language for the manager to use verbatim — only suggest *evidence-and-question* prompts the manager can adapt

The system MAY:
- Suggest a coaching focus area based on the evidence ("the pattern here is around discovery depth")
- Provide call-clip excerpts the manager can review before the conversation
- Track whether coaching conversations occurred (timestamp + manager-marked) for outcome research
- Flag when a rep has not received any coaching conversation in N weeks

## 3 · Manager-executable coaching framework

The manager's coaching conversation is a 15-30 minute structured discussion grounded in an observed call moment. The system supplies the evidence; the manager facilitates the discussion.

### The four-step conversation pattern

**Step 1 — Open with what worked (3-5 min).** Manager describes one observed strength with evidence. ("In the call with Acme on Tuesday, when the customer pushed back on pricing, you reframed the conversation around their stated ROI goal. That was strong.") This is not flattery; it is grounding the conversation in evidence the rep recognizes.

**Step 2 — Surface the focus area as observation, not judgment (3-5 min).** Manager shares a second piece of evidence. ("Later in the same call, you moved to the demo right after the pricing reframe. I want to think through that moment with you.") No diagnosis attached.

**Step 3 — Ask, don't tell (10-15 min).** Manager asks open questions. The rep often identifies the gap themselves; if not, the manager guides. Examples:
- "What were you noticing about the customer at that moment?"
- "What were you trying to accomplish?"
- "What would you do differently if the moment came up again?"
- "What would you want to know from the customer before moving to the demo?"

**Step 4 — Co-create the next experiment (5 min).** Together they pick one specific behavior to try in the next 1-2 calls and a way to check in. Not three things; one. Specific. ("Next week, on the discovery calls with the two demos in your pipeline, try one Implication question before pitching solution. We'll talk about how it went on Friday.")

### What the system provides the manager

For each queue item:

- One paragraph of context (call, customer, what triggered the queue item)
- Two evidence excerpts: one strength, one focus area, both timestamped and clip-playable
- 2-3 candidate open questions for Step 3 (managers adapt; not delivered verbatim)
- 1-2 candidate next-experiment suggestions for Step 4 (managers adapt)
- A "mark this conversation done" button that captures: date, focus area, agreed-on experiment, optional notes

### What the system does NOT provide

- A coaching script to read aloud
- A numeric "rep grade" the manager is expected to communicate
- An assessment of whether the rep is meeting / exceeding / failing expectations
- Recommended performance-management actions (PIP, etc.)

### Outcome-tracking

For research and continuous improvement, the system records:

- Whether the conversation occurred (manager marked or not)
- Days from queue surfacing to conversation
- Focus area chosen (if any)
- Subsequent call performance on the focus-area metric (4-week rolling)
- Rep retention and self-reported coaching satisfaction over time

This data informs whether the queue is generating useful conversations, whether the coaching focus areas correlate with later improvement, and whether certain coaching framings work better than others. This is research data, not performance-management data — it is admin-visible only and never tied to individual manager evaluations.

## Implementation order

1. Build the data-capture layer first: review queue with manual triage, coaching-conversation-occurred tracking, focus-area logging. No automated triage yet. Get 4-8 weeks of manager behavior data.
2. Layer in the always-surface and pattern-detection triage rules from §2. Tune thresholds against actual manager engagement (do they review when surfaced? do they mark Done? does follow-up happen?).
3. Add the suggestion layer (candidate open questions, candidate experiments). A/B test against a control where the manager only sees the evidence.
4. Add outcome tracking (call-performance correlation, rep retention) once Phase 0 telemetry is mature enough to support it.

## Open questions to resolve before building

- Per-tenant configurability: do tenants get to set their own triage thresholds, or do we ship one default and tune from data?
- Manager workload: how many queue items per manager per week is sustainable? (The literature suggests 3-5 coaching conversations per rep per quarter.)
- Rep-side visibility: can the rep see what's in the queue about them? (Recommend: no, by default — the queue is the manager's working tool, not a performance-tracking surface for the rep — but allow tenants to opt into transparency if their culture supports it.)
- Tenant cultural fit: some sales orgs are adversarial in their coaching style; this framework is rooted in growth-oriented coaching. Make sure the language we suggest doesn't read as soft to those tenants while still preserving the research-backed structure. Consider tenant-level coaching-tone presets ("growth", "balanced", "performance-driven").
