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

### 4.3 Open pricing questions to resolve before launch

1. **Floor overage price** above 5K min/seat/mo — cost-plus dictates ≥$0.02/min to
   preserve margin; Dubber and MiaRec include large pools and meter only on extreme
   overage.
2. **Annual-commit discount percentage** — 20% is market-standard (Otter, Avoma,
   Fireflies); 25–30% would move against channel partners more aggressively.
3. **Minimum seat counts** — competitor norm is 3–10 seats for mid-market,
   25–50 for Enterprise. Current plan has no minimum.
4. **Add-on SKUs vs. tier gating** — MiaRec gates at tier; Avoma uses stackable
   add-ons ($29 CI, $39 Revenue). Add-ons are friendlier for product-led growth.
5. **White-label partner pricing (per-seat wholesale)** — separate exercise; typical
   is 50–70% of list to channel partner with partner setting end-customer price.

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
