# CallSight AI — Pricing Models (v1 Draft)

Two per-seat pricing models for CallSight AI, tiered by **feature set** (not usage).
Both models assume a **5,000 audio-minute/seat/month cap**, billed monthly with annual-
commit discounts. Ranges below are **low / high**; pick a point inside the band based
on segment, sales motion, and channel margin requirements.

- **Model A — Cost-Plus (competitor-agnostic):** what it will realistically cost us to
  deliver each tier, marked up to a 75–80% gross-margin target.
- **Model B — Competitor-Anchored:** what comparable channel-partner / white-label
  vendors charge for equivalent feature bundles (SimpleVOIP, MiaRec, Dubber, Xima,
  Symbl.ai, 8x8).

A final "Recommended Launch Pricing" table reconciles both at the bottom.

---

## 1. Product Cost Model

### 1.1 Cost drivers inventoried from the codebase

From `backend/app/services/*.py` and `requirements.txt`:

| Subsystem | Source file(s) | External cost driver |
|---|---|---|
| Batch + streaming transcription | `transcription.py` | Deepgram Nova-3 (`$0.0043/min` batch, `$0.0077/min` streaming) **or** self-hosted Whisper large-v3 GPU (`~$0.0008–$0.0015/min` amortized on spot) |
| Speaker diarization | `transcription.py` | Included free in Deepgram; `pyannote.audio` GPU seconds if Whisper path |
| AI deep analysis | `ai_analysis.py` | Claude Sonnet 4.6 (`$3/MTok in, $15/MTok out`) w/ prompt caching (90% read discount) |
| AI fast triage / live coaching | `triage_service.py`, `live_coaching.py` | Claude Haiku 4.5 (`$1/MTok in, $5/MTok out`) |
| Scorecard / QA | `scorecard_service.py` | Haiku w/ cached rubric |
| PII redaction | `pii_redaction.py` | Presidio + spaCy (CPU); negligible marginal cost |
| Knowledge-base RAG | `knowledge_base.py` (api) + Qdrant | Qdrant vectors + Anthropic embeddings (negligible per-seat) |
| Transcript search | `search_service.py` | Managed Elasticsearch cluster |
| Tenant insights rollups | `tenant_insights_service.py` | Claude Haiku batch nightly |
| Snippet extraction, script tracking | `snippet_service.py`, `script_tracker.py` | CPU only |
| Audio storage | `backend/app/services/` (S3) | S3 Standard → Glacier lifecycle |
| Realtime WS, notifications, webhooks | `websocket.py`, `notification_service.py`, `webhook_dispatcher.py` | ALB hours, Redis pub/sub, egress |
| Platform base | `db.py`, `main.py`, `auth.py` | Postgres (Neon/RDS), Redis, FastAPI compute |

### 1.2 Per-seat unit cost at 5,000 min/mo cap

All figures are **$/seat/month**. "Low" assumes self-hosted Whisper + Haiku-first
routing + aggressive prompt caching + reserved infra. "High" assumes 100% Deepgram
Nova-3 + Sonnet for all deep analyses + on-demand infra.

| # | Component | Low | High | Feature it unlocks |
|---|---|---|---|---|
| 1 | Transcription + diarization (5K min) | $5.00 | $38.50 | All tiers (base) |
| 2 | Fast triage (Haiku, ~333 calls/seat) | $0.30 | $1.00 | All tiers |
| 3 | Deep analysis (Sonnet, cached) | $6.50 | $18.00 | Pro tier + |
| 4 | Premium analysis (Opus on ~15% of calls) | $0.00 | $14.00 | Enterprise only |
| 5 | Live coaching (Haiku streaming) | $0.00 | $12.00 | Business + |
| 6 | Auto-QA / scorecards (100% of calls) | $0.00 | $4.00 | Business + |
| 7 | PII redaction (Presidio self-hosted) | $1.00 | $4.00 | Pro tier + |
| 8 | KB / RAG (Qdrant + embeddings) | $0.40 | $1.50 | Pro tier + |
| 9 | Elasticsearch transcript search | $0.80 | $3.50 | Pro tier + |
| 10 | S3 audio + lifecycle | $1.00 | $2.00 | All tiers |
| 11 | Postgres + Redis | $1.50 | $4.50 | All tiers |
| 12 | API / worker compute (ECS/K8s) | $3.00 | $7.50 | All tiers |
| 13 | WebSocket realtime infra | $0.40 | $2.00 | Business + |
| 14 | Integrations + outbound webhooks | $0.40 | $1.20 | Pro tier + |
| 15 | Multi-tenant / custom-domain / SSL | $0.40 | $1.00 | All tiers |
| 16 | Observability (Datadog/Sentry/logs) | $0.80 | $2.50 | All tiers |
| 17 | Support + CS amortized per seat | $2.00 | $15.00 | Scales with tier |

Notes:
- Transcription dominates: at 5K min/seat, Deepgram streaming alone is ~$38.50.
  Tier design must route low-tier seats to Whisper or Deepgram batch.
- Claude costs are dominated by input tokens, so prompt caching (already in
  `ai_analysis.py`) is load-bearing. Without caching, Sonnet costs 3–4× higher.
- CS cost varies from <$2 self-serve Starter to $15+ for white-glove Enterprise.

---

## 2. Model A — Cost-Plus (Competitor-Agnostic)

Target gross margin: **75–80%** (business plan anchor). Price = COGS ÷ (1 − margin).
Tier floor is a blended COGS assuming realistic engine routing for that tier.

### 2.1 Tier build-up

| Component group | Starter | Professional | Business | Enterprise |
|---|---|---|---|---|
| Transcription engine | Whisper SH only | Deepgram batch | Deepgram batch + live streaming | Deepgram premium + on-prem option |
| AI analysis | Haiku only | Sonnet w/ cache | Sonnet + Opus blend | Full Sonnet/Opus + custom prompts |
| Live coaching | — | — | ✔ (Haiku stream) | ✔ |
| Scorecards / Auto-QA | — | Sampled (10%) | 100% of calls | 100% + custom rubrics |
| PII redaction | — | ✔ | ✔ | ✔ + BYO key |
| KB / RAG | — | ✔ | ✔ | ✔ |
| Integrations | Email only | 2 CRM + Slack | Unlimited CRM + webhooks | Custom SI + SSO/SAML |
| Dedicated infra | Shared | Shared | Shared | Dedicated tenant pod |
| Support | Community | Email (business hrs) | Priority + CSM | Dedicated CSM + SLA |

### 2.2 COGS per seat (fully-loaded at 5K min cap)

| Tier | Low COGS | High COGS |
|---|---|---|
| Starter | $10 | $18 |
| Professional | $22 | $48 |
| Business | $42 | $82 |
| Enterprise | $78 | $145 |

### 2.3 Target price ranges

Using `price = COGS / (1 − margin)` for both 75% and 80% margin, then rounding to a
sellable $/seat/mo band:

| Tier | 75% margin (low COGS) | 75% margin (high COGS) | 80% margin (high COGS) | **Recommended list** |
|---|---|---|---|---|
| Starter | $40 | $72 | $90 | **$29–49/seat/mo** |
| Professional | $88 | $192 | $240 | **$89–149/seat/mo** |
| Business | $168 | $328 | $410 | **$199–299/seat/mo** |
| Enterprise | $312 | $580 | $725 | **$449–699/seat/mo** (often custom) |

**What this tells us:** at a true 5K min/seat/mo cap and 80% target margin, the math
pushes Professional to **$150–240** and Business to **$300–400** — meaningfully higher
than the current business-plan SKUs ($49 Pro / $89 Business). Closing that gap requires
one of: (a) lower included-minutes caps for the lower tiers, (b) Whisper-only routing
in Starter/Pro, or (c) accepting ~65–70% gross margin on lower tiers and making it up
on Business/Enterprise.

---

## 3. Model B — Competitor-Anchored (Channel Partners)

### 3.1 Anchor set

Per-seat pricing for channel-partner / white-label / embedded vendors that compete on
the same buyer and feature set:

| Vendor | Channel model | Entry tier | Top CI tier | CI features at top tier |
|---|---|---|---|---|
| **SimpleVOIP** (Simple Intelligence) | White-label MSP | Quote-gated | Quote-gated | Transcription, sentiment, intent, commitment tracking, CRM push |
| **MiaRec** | CCaaS partner / OEM | **$99** (CX Intelligence) | **$129+** (Revenue Intelligence) | Auto-QA, sentiment, buying signals, Ask AI |
| **Dubber** | Carrier-embedded OEM | **~$19** (Pro) | **~$39–$40** (Ent / Teams) | Notes, Moments, transcription, sentiment, search |
| **Xima** (Chronicall) | Mitel/Avaya/3CX dealer | **$50** (Essentials) | **$68** (Pro); $40 Elite (promo) | Speech analytics, AI QM, transcription, messaging AI |
| **Symbl.ai** (Invoca, May 2025) | API embed / white-label | $0 free / $0.027/min | Enterprise custom | Transcription, sentiment, intents, real-time assist, Call Score |
| **8x8** (X-series) | Master-agent channel | **$24** (X2, voice only) | **$140** (X8, CC + speech analytics) | Speech Analytics + Quality Management at X8 |

Symbl's per-minute PAYG converts to **~$85–$135/seat/mo** at our 5K cap — a useful
embed-model benchmark.

### 3.2 Feature-parity mapping

For each CallSight tier, the closest-parity competitor SKU and its price:

| CallSight tier | Closest competitor parity | Their price | Our positioning |
|---|---|---|---|
| Starter | Dubber Pro (recording + light AI) | ~$19/seat/mo | Match or slightly undercut |
| Professional | Xima Essentials–Pro / 8x8 X4 | $44–$68/seat/mo | Match mid-band |
| Business | MiaRec CX Intelligence / 8x8 X6 | $85–$99/seat/mo | Match with feature depth advantage |
| Enterprise | MiaRec Revenue Intel / 8x8 X8 | $129–$140/seat/mo | Match to slight premium for dedicated + SSO |

### 3.3 Competitor-anchored price ranges

| Tier | Competitor-anchored low | Competitor-anchored high |
|---|---|---|
| Starter | $19/seat/mo | $29/seat/mo |
| Professional | $49/seat/mo | $75/seat/mo |
| Business | $89/seat/mo | $119/seat/mo |
| Enterprise | $139/seat/mo | $199/seat/mo |

**What this tells us:** Every competitor-anchored tier except Enterprise lands
**below** the cost-plus 75%-margin floor at a fully-loaded 5K min/seat. Viable only
if we route aggressively to Whisper + Haiku for Starter/Pro and accept that actual
per-seat minute usage will average ~1,500–2,500 (not the 5K cap), giving blended
margins closer to target.

---

## 4. Side-by-Side & Recommended Launch

### 4.1 Comparison

| Tier | Cost-Plus (A) | Competitor-Anchored (B) | Gap |
|---|---|---|---|
| Starter | $29–49 | $19–29 | A is ~1.5× higher |
| Professional | $89–149 | $49–75 | A is ~1.8–2× higher |
| Business | $199–299 | $89–119 | A is ~2.2–2.5× higher |
| Enterprise | $449–699 | $139–199 | A is ~3× higher (but dedicated infra) |

### 4.2 Recommended launch pricing

Blend the two models: anchor Starter/Professional to competitor bands (taking near-
term margin compression as a customer-acquisition tradeoff) and keep Business and
Enterprise near the cost-plus floor because (a) competitors converge higher there
and (b) feature depth (live coaching + 100% auto-QA + custom domains + SSO) justifies
a premium.

| Tier | Launch list price | Annual-commit price | Blended GM at avg usage* |
|---|---|---|---|
| **Starter** | $39/seat/mo | $29/seat/mo | ~55–65% (loss-leader) |
| **Professional** | $89/seat/mo | $69/seat/mo | ~70–75% |
| **Business** | $179/seat/mo | $149/seat/mo | ~77–82% |
| **Enterprise** | $349+/seat/mo (custom) | $299+/seat/mo | ~80–85% |

\* Assumes realized usage averages ~2,000 min/seat/mo (not the 5K cap); blended GM
drops proportionally as usage approaches the cap.

### 4.3 Pricing questions — resolved in v2

All five open questions from the v1 draft have now been resolved by the business
owner. Each is documented in a v2 section below:

1. **Floor overage price** above 5K min/seat/mo — **Resolved: $0.02/min**, expected
   to be rarely hit at realistic usage. See §6.1 and §8.2.
2. **Annual-commit discount percentage** — **Resolved: 20% standard, 25% on 2-year
   commits**, with an explicit posture of undercutting the competitor band at
   every tier. See §6.2.
3. **Minimum seat counts** — **Resolved: three seat bands** (1–10 / 11–25 / 25+)
   with no self-serve floor on Starter/Pro, 3-seat min on Business, 10-seat min
   on Enterprise. See §6.1 and §6.3.
4. **Add-on SKUs vs. tier gating** — **Resolved: both**. Tiers gate features,
   but individual higher-tier features are available as add-on SKUs priced
   ~30–40% above bundled cost so 2+ add-ons push toward a tier upgrade. See §8.
5. **White-label partner pricing (per-seat wholesale)** — **Resolved: full
   channel program** with three partner tiers (Authorized Reseller 35% / Premier
   45% / Elite 55–60%), three packaging options, deal registration, MDF, and
   worked economics. See §9.

---

## 5. Summary

- **Cost-Plus model** (competitor-agnostic) at our fully-loaded 5K min/seat cap says
  Professional should list at **$89–149** and Business at **$199–299** to hit 75–80%
  gross margin.
- **Competitor-Anchored model** (channel partners: SimpleVOIP, MiaRec, Dubber, Xima,
  Symbl.ai, 8x8) says the market clears at **$49–75 (Pro)** and **$89–119 (Business)**.
- The two reconcile only if (a) real per-seat usage averages ~40% of the 5K cap,
  (b) Starter/Pro route mostly to Whisper + Haiku, and (c) higher tiers cross-subsidize
  a thinner-margin Starter.
- Recommended launch list: **Starter $39 / Pro $89 / Business $179 / Enterprise $349+**,
  with 20–25% annual-commit discounts.

---

# v2 Additions — Seat Bands, Customer-Facing Tiers, Add-Ons, White-Label

Sections 6–10 build on the v1 reconciled recommendation by adding seat-band
pricing, customer-facing tier descriptions (plain-English, sales-ready), a
per-feature add-on price list, and the white-label channel program for
distributors like Reinvent Telecom.

---

## 6. Seat-Band Tier Structure

Per-seat pricing scales **down** as seat count increases — smaller deployments
pay more per seat (standard SMB premium); larger deployments get volume pricing.
Every tier has three bands:

- **SMB** — 1–10 seats (self-serve or light-touch sales)
- **Mid-Market** — 11–25 seats (assisted sales, standard onboarding)
- **Growth** — 25+ seats (custom terms, volume-discounted, annual commit encouraged)

### 6.1 Month-to-month list price ($/seat/mo)

| Tier | 1–10 seats (SMB) | 11–25 seats (Mid-Market) | 25+ seats (Growth) |
|---|---|---|---|
| **Starter** | $39 | $34 | $29 |
| **Professional** | $89 | $79 | $69 |
| **Business** | $179 | $159 | $139 |
| **Enterprise** | Custom | Custom | Custom (from $299/seat) |

### 6.2 Annual-commit price (20% off; 25% off 2-year)

| Tier | 1–10 (1-yr) | 11–25 (1-yr) | 25+ (1-yr) | 25+ (2-yr) |
|---|---|---|---|---|
| **Starter** | $31 | $27 | $23 | $22 |
| **Professional** | $71 | $63 | $55 | $52 |
| **Business** | $143 | $127 | $111 | $104 |
| **Enterprise** | — | — | $239+ | $224+ |

All bands include the **5,000 audio-minute/seat/month cap**. Overage billed at
**$0.02/min**, trued-up monthly — rarely hit at realistic usage.

### 6.3 Band-selection rules

- Band is set by **current paid seat count**, evaluated each billing cycle.
- **Grandfathering:** customers crossing a band threshold keep their current
  rate on existing seats for 12 months; only new seats price into the new band.
- **Minimums:** no floor on Starter / Pro self-serve; 3-seat min on Business;
  10-seat min on Enterprise.

---

## 7. Customer-Facing Tier Overview

Plain-English feature guide for sales collateral, partner enablement, and the
website pricing page. Written for end-customer comprehension — use verbatim.

### 7.1 Starter — "Get the basics right"

**Who it's for:** Small teams that want accurate call transcripts, searchable
recordings, and one place to review conversations — without the complexity of
a full AI platform.

**What you get:**
- Automatic transcription of every call (up to 5,000 minutes per seat, per month)
- Speaker separation — see who said what on two-party calls
- Full-text transcript search across your last 90 days of calls
- AI-generated call summaries so you can skip listening to the full recording
- Secure cloud storage of every call recording and transcript
- Mobile and web apps for reps and managers
- Email support during business hours

**Not included:** live coaching during calls, automated QA scorecards, CRM
write-back, PII redaction, custom analytics, and integrations beyond email.
Add these à la carte (see Section 8) or upgrade to Professional.

---

### 7.2 Professional — "Coach smarter, sell more"

**Who it's for:** Revenue teams that want AI to review every conversation,
surface what's working, and push insights into the CRM they already use.

**Everything in Starter, plus:**
- **AI deep-dive analysis** on every call — buying signals, objections, next
  steps, talk-to-listen ratio, commitments
- **Automatic PII redaction** — credit cards, SSNs, and other sensitive data
  stripped from transcripts and summaries before storage
- **Knowledge-Base Q&A** — ask "what did Acme say about pricing last quarter?"
  across your entire call history
- **CRM integrations** — two-way sync with Salesforce, HubSpot, or Zoho; auto-
  log calls, summaries, and action items
- **Slack notifications** — deal-risk alerts and coaching moments pushed to the
  right channel in real time
- **Sampled auto-QA scorecards** — 10% of calls automatically scored against
  your rubric
- **Extended retention** — 12 months of searchable transcripts and recordings
- **Email + chat support**, responses within 1 business day

---

### 7.3 Business — "Run a revenue machine"

**Who it's for:** Sales and CS teams that treat conversation intelligence as
core infrastructure — coaching every rep, scoring every call, and catching deal
risk the moment it happens.

**Everything in Professional, plus:**
- **Live AI coaching** — real-time suggestions in the rep's ear during live
  calls: objection handling, next-best-question, competitor mentions, compliance
  prompts
- **100% auto-QA** — every call scored against your scorecard, with trend
  charts for each rep, team, and product line
- **Custom scorecards and rubrics** — build your own QA categories with
  natural-language rules the AI enforces
- **Unlimited CRM / tool integrations** plus outbound webhooks to any system
- **Snippet library** — auto-extracted customer quotes, objections, and
  testimonials, organized by theme
- **Script adherence tracking** — see which reps are following the playbook
  and where calls go off-script
- **Deal-risk dashboards** for sales leadership with automated rollups
- **Priority support** — 4-hour response SLA, dedicated Slack channel

---

### 7.4 Enterprise — "Built around your business"

**Who it's for:** Large or regulated organizations that need dedicated
infrastructure, compliance guarantees, and the ability to shape the platform
to their workflow.

**Everything in Business, plus:**
- **Dedicated tenant** — your own isolated infrastructure, not shared
- **Custom domain + fully branded UI** — your logo, colors, and domain
- **SSO / SAML / SCIM** with Okta, Azure AD, Google Workspace, and any SAML IdP
- **Bring-your-own AI key** — point the platform at your Anthropic, Azure
  OpenAI, or AWS Bedrock account for data-residency or procurement reasons
- **Custom AI prompts** — our team tunes the analysis and scorecard prompts to
  your industry, playbook, and compliance needs
- **On-prem / VPC transcription option** — keep audio inside your network
- **SOC 2 Type II, HIPAA BAA, GDPR DPA** — included
- **Uptime SLA** — 99.9% with service credits
- **Dedicated Customer Success Manager** and business reviews
- **24/7 support**, 1-hour response SLA

---

## 8. Add-On SKU Price List

For Starter and Professional customers who need a single higher-tier capability
without upgrading the whole plan. Add-ons are **priced ~30–40% above the bundled
per-feature cost** — rational for one feature, irrational for two or more (sales
should steer to the next tier at that point).

### 8.1 Per-seat add-ons (billed per seat, per month)

| Add-on | Price | Available on | What it unlocks |
|---|---|---|---|
| **AI Deep Analysis** | $19/seat/mo | Starter | Sonnet-powered per-call analysis (buying signals, objections, next steps) |
| **PII Redaction** | $9/seat/mo | Starter | Automatic redaction of PII from transcripts and summaries |
| **Knowledge-Base Q&A** | $12/seat/mo | Starter, Pro | Natural-language search across call history |
| **CRM Integration Pack** | $19/seat/mo | Starter | Salesforce, HubSpot, Zoho two-way sync |
| **Live AI Coaching** | $29/seat/mo | Starter, Pro | Real-time in-call coaching (Haiku streaming) |
| **100% Auto-QA + Scorecards** | $25/seat/mo | Starter, Pro | Every call scored; standard rubric |
| **Priority Support** | $19/seat/mo | Starter, Pro | 4-hour response SLA, dedicated Slack channel |
| **Extended Retention — 24 months** | $9/seat/mo | Starter, Pro | Searchable transcripts retained 2 years |
| **Extended Retention — 7 years (compliance)** | $19/seat/mo | Any | Legal-hold / regulated industries |

### 8.2 Tenant-wide add-ons (billed flat, per month)

| Add-on | Price | Available on | What it unlocks |
|---|---|---|---|
| **SSO / SAML / SCIM** | $99/mo flat | Pro, Business | Okta, Azure AD, Google; user auto-provisioning |
| **Custom Domain + Branded UI** | $49/mo flat | Pro, Business | your-brand.callsight.ai or your own domain |
| **Bring-Your-Own AI Key** | $49/mo flat | Pro, Business | Route AI spend through your Anthropic / Bedrock account |
| **Custom Scorecard Authoring** | $149/mo flat | Pro, Business | Self-serve builder for custom QA rubrics |
| **Dedicated Success Manager** | $499/mo flat | Pro, Business | Named CSM, monthly sync, platform reviews |
| **Additional Audio Storage** | $0.02/GB/mo | Any | Beyond the 50 GB/seat included |
| **Usage Overage** | $0.02/min | Any | Beyond the 5,000-min/seat/mo cap |

### 8.3 Worked example — when an add-on beats an upgrade

- **Pro + Live Coaching** ($29) = $118/seat/mo — still cheaper than Business
  $179. Rational if the team needs coaching but not 100% QA, snippet library,
  or script tracking.
- **Pro + Live Coaching ($29) + 100% Auto-QA ($25)** = $143/seat/mo — now
  within $36 of Business list price ($179), and Business additionally includes
  unlimited integrations, custom scorecards, snippet library, script tracking,
  and priority support. At this point, upgrade.

---

## 9. White-Label / Channel Partner Program

**Strategic context:** The majority of CallSight revenue is expected to come
through white-label distribution — partners like Reinvent Telecom and competing
master agents / UCaaS distributors who resell CallSight under their own brand
as part of their own product catalog. This program is a first-class SKU, not
an afterthought.

### 9.1 Partner tier structure

Partners qualify into three tiers based on committed annual recurring revenue
(ARR) or seat volume. Tier sets wholesale discount, co-marketing support, and
service expectations.

| Partner tier | Qualification | Wholesale discount off list | Deal-reg bonus | MDF | Tier 2/3 support |
|---|---|---|---|---|---|
| **Authorized Reseller** | 50–250 seats active, $25K+ ARR committed | **35% off list** | +5% on registered deals | — | CallSight handles |
| **Premier Partner** | 250–1,000 seats active, $150K+ ARR committed | **45% off list** | +5% on registered deals | 2% of quarterly revenue | CallSight handles Tier 3 only |
| **Elite Distributor** | 1,000+ seats active, $500K+ ARR committed | **55% off list** (up to **60%** with 2-yr commit) | +7% on registered deals | 4% of quarterly revenue | Partner handles Tier 1–2; CallSight Tier 3 |

Partner discounts apply to the **list-price band the end customer qualifies
for** (1–10 / 11–25 / 25+). Partner sets end-customer price; wholesale COGS to
partner is fixed by partner tier.

### 9.2 Wholesale price examples — Premier Partner (45% off list)

| Tier | 1–10 list | Partner pays | 11–25 list | Partner pays | 25+ list | Partner pays |
|---|---|---|---|---|---|---|
| Starter | $39 | **$21.45** | $34 | **$18.70** | $29 | **$15.95** |
| Professional | $89 | **$48.95** | $79 | **$43.45** | $69 | **$37.95** |
| Business | $179 | **$98.45** | $159 | **$87.45** | $139 | **$76.45** |
| Enterprise | — | custom (min $299 list → $164 partner) | — | — | — | — |

Partners typically mark up 40–80% over wholesale. The discount structure lets
an Elite Distributor clear 50%+ margin while still undercutting MiaRec, Dubber,
and Xima on end-customer pricing.

### 9.3 White-label packaging options

Three packaging choices; partner picks one at onboarding and can upgrade.

| Packaging | Branding | Partner handles | CallSight handles | Fee |
|---|---|---|---|---|
| **Co-branded ("Powered by CallSight")** | Partner logo + "Powered by CallSight" footer | Sales, Tier 1 support, billing | Platform, Tier 2/3, security, uptime | $0 (included at Premier+) |
| **Standard White-Label** | Partner brand only; CallSight invisible to end customer | Sales, Tier 1 support, billing, first-line CS | Platform, Tier 3, security, uptime | $199/mo platform fee (Authorized Reseller); included for Premier+ |
| **Full White-Label + Custom Domain** | Partner brand on partner-owned domain; fully private-labeled | Sales, Tier 1–2, billing, CS, onboarding | Platform, Tier 3, security, uptime, AI models | $499/mo platform fee + $99/mo per custom domain (included for Elite) |

### 9.4 Partner economics worked example

Reinvent Telecom onboards as **Elite Distributor** (1,000+ seats, 2-year commit,
60% wholesale discount). They deploy CallSight to a 50-seat end customer on
Business tier, 11–25 seat band ($159 list):

- End customer pays Reinvent: $159/seat/mo
- Reinvent pays CallSight: $159 × (1 − 0.60) = **$63.60/seat/mo wholesale**
- Reinvent gross margin: **$95.40/seat/mo (60%)** → $57,240/yr on this one
  account
- CallSight recurring revenue: **$38,160/yr** from this single Reinvent
  subagent sale
- Across 10 such deployments, one Elite partner drives **~$381K ARR** to
  CallSight with minimal direct-sales cost.

### 9.5 Deal registration

- Partners register opportunities in the partner portal **before** first
  customer meeting.
- Registered deals protected for 90 days, renewable once.
- Deal-reg bonus (5–7% additional discount) applies for the life of the
  contract on registered deals.
- Conflict policy: partner-first. If CallSight direct can't show earlier
  documented activity, the partner wins the deal.

### 9.6 Onboarding and enablement

| Element | Included at | Notes |
|---|---|---|
| Partner portal (deal reg, reporting, billing) | All tiers | Self-serve |
| Product certification (2 engineers, 4 hrs) | All tiers | Required before first sale |
| Sales enablement kit (decks, battlecards, ROI tool) | All tiers | Refreshed regularly |
| Co-selling sales assist | Premier+ | CallSight AE joins partner calls |
| Business-review cadence | Premier+ | Pipeline, forecast, roadmap preview |
| Roadmap input | Elite | Partner feature requests enter product planning |
| Co-marketing campaigns | Premier+ | MDF-funded |
| Event / conference co-presence | Elite | Shared booth at master-agent events |

### 9.7 Mutual commitments

**Partner commits to:**
- Ramp against the committed ARR schedule set at contract signature
- At least 2 certified engineers per 100 partner-deployed seats
- Tier-1 support per the packaging option selected
- No cross-selling to CallSight direct-list customers without coordination

**CallSight commits to:**
- Named partner manager (Premier+) / shared pool (Authorized Reseller)
- Wholesale pricing locked for term of contract
- 90-day notice on any material price or feature change
- Partner-first conflict resolution on registered deals
- Technical escalation SLA: Tier-3 response ≤ 4 hrs (Premier+) / ≤ 1 hr (Elite)

### 9.8 Target partner mix

First wave targets a small number of Elite Distributors (Reinvent Telecom
included), a broader set of Premier Partners, and an open Authorized Reseller
program for any qualified UCaaS / MSP. Elite partners are expected to drive the
bulk of revenue; Authorized Resellers expand logo count and market coverage.
**Ramp timing and partner-signing cadence are the CallSight team's call — the
program structure itself is ready whenever you decide to open it.**

---

## 10. Summary (v2)

- **Seat bands** (1–10 / 11–25 / 25+) with per-seat pricing declining across
  bands: Starter $39→$29, Pro $89→$69, Business $179→$139, Enterprise custom
  from $299.
- **20% annual-commit discount** standard; **25%** for 2-year commits. Intent
  is to price under the competitor band at every tier.
- **Add-on SKUs** let Starter / Pro customers unlock individual higher-tier
  features at a ~30–40% premium — rational for one feature, pushes to upgrade
  at two or more.
- **White-label program** with three partner tiers (Authorized Reseller 35%,
  Premier 45%, Elite 55–60%), three packaging options (co-branded, standard
  white-label, full white-label + custom domain), deal registration, MDF, and
  partner-first conflict policy. Designed for Reinvent-class distributors as
  first-class revenue channels.
- **Overage:** $0.02/min above the 5K-min/seat cap; rarely hit at realistic
  usage.
