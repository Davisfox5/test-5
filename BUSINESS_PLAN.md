# CallSight AI — Business & Marketing Plan

## 1. Executive Summary

CallSight AI is a white-label B2B SaaS platform that automatically transcribes customer calls and uses AI to generate actionable follow-ups, coaching insights, and sentiment analysis. Sold to companies running sales and customer-support operations, the platform is designed to be resold under their own brand to their end customers or used internally to drive team performance.

The conversation intelligence market is projected to exceed $40B by 2030. CallSight targets the underserved mid-market segment — companies with 10–500 reps — that are priced out of enterprise solutions like Gong and Chorus but need more than basic call recording. Our white-label model creates a second revenue stream by enabling telephony providers, CRM vendors, and BPOs to embed call intelligence as a native feature.

---

## 2. Problem Statement

| Pain Point | Who Feels It | Current Workaround |
|------------|-------------|-------------------|
| Reps forget action items from calls | Sales & support teams | Manual note-taking, post-call CRM entry |
| Managers can't review every call | Sales leaders | Random call sampling, anecdotal feedback |
| No structured follow-up process | Revenue teams | Inconsistent follow-up, lost deals |
| Customers repeat themselves across calls | Support orgs | Agents re-read ticket history manually |
| Telephony/CRM platforms lack built-in intelligence | SaaS vendors | Buy/build separate tooling, poor UX |

**Core insight:** The value isn't in the transcript — it's in what happens *after* the call. Most platforms stop at transcription. CallSight closes the loop by turning conversations into assigned, tracked action items that flow directly into existing workflows.

---

## 3. Target Market

### 3.1 Primary Segments

| Segment | Profile | Deal Size (ARR) | Volume |
|---------|---------|-----------------|--------|
| **Mid-market Sales Teams** | 10–500 reps, B2B sales orgs, SDR/AE teams | $12K–$120K | High |
| **Customer Support / Contact Centers** | 20–1,000 agents, inbound call centers | $24K–$300K | High |
| **White-Label Partners** | Telephony providers, CRM vendors, BPOs | $50K–$500K+ | Lower count, higher value |

### 3.2 Ideal Customer Profile (Direct Sales)

- **Company size:** 50–500 employees
- **Annual revenue:** $10M–$500M
- **Industries:** SaaS, financial services, insurance, healthcare, real estate, professional services
- **Tech stack:** Already using Salesforce/HubSpot, Zoom/Teams, Twilio/RingCentral
- **Decision makers:** VP Sales, VP Customer Success, CRO, Head of Support
- **Budget:** $50–$150/seat/month

### 3.3 Ideal White-Label Partner

- **CPaaS providers** (Twilio competitors, regional telecoms) wanting to add AI features
- **CRM vendors** (vertical CRMs) wanting native call intelligence
- **BPO / outsourced call centers** wanting to offer analytics to their clients
- **Revenue intelligence resellers** wanting a platform they can brand

---

## 4. Competitive Analysis

Our real competitors are not consumer transcription tools — they are enterprise speech analytics platforms, contact center AI vendors, conversation intelligence API providers, and cloud hyperscaler offerings that serve the same buyers and partners we target.

### 4.1 Competitive Landscape Map

```
                    FULL PLATFORM (turnkey)
                            │
     NICE · Verint          │        Cresta · Balto
     CallMiner · Calabrio   │        Observe.AI · Uniphore
                            │
 LEGACY/INCUMBENT ──────────┼──────────── AI-NATIVE/MODERN
                            │
     Genesys · Talkdesk     │     ★ CallSight AI ★
     AWS Contact Lens       │        Symbl.ai
     Google CCAI · Nuance   │
                            │
                    API / EMBEDDABLE LAYER
                            │
                   Deepgram · AssemblyAI
                   Rev.ai · Speechmatics
```

### 4.2 Competitor Deep-Dive

#### Category A: Enterprise Speech Analytics Platforms

These are the incumbents — massive, entrenched in Fortune 500 contact centers, with long sales cycles and high price tags. They are our competitors for enterprise deals but not for the white-label/embedded channel.

**NICE (CXone Enlighten AI)** — $$$$$
- **What they offer:** End-to-end contact center platform with Enlighten AI for sentiment, coaching, QA automation, and agent copilot. Trained on 30+ years of CX data — the industry's largest proprietary dataset.
- **White-label/OEM:** No. NICE sells its own branded platform. Partners resell NICE, not their own brand.
- **Target:** Large enterprise contact centers (500–50,000+ agents)
- **Pricing:** Custom enterprise contracts, typically $150K–$1M+ annually
- **Strengths:** Market leader in contact center analytics (Gartner Leader), deepest analytics, massive training data, full compliance suite, 8,000+ enterprise customers
- **Weaknesses:** Extremely complex implementation (3–6+ months), rigid platform — customers must adopt the full CXone stack, no API-first or embedded option, priced out of mid-market
- **Our angle:** We compete on speed-to-deploy, embeddability, and the mid-market. NICE can't be white-labeled; we can. NICE requires a full platform swap; we plug into existing stacks.

**Verint (Open Platform + Speech Analytics)** — $$$$$
- **What they offer:** Enterprise speech and text analytics with the "Exact Transcription Bot," sentiment scoring, PII redaction, and generative AI insights via "Genie Bot." Frost & Sullivan named them market leader in VoC analytics (2025).
- **White-label/OEM:** Limited. Verint's open platform supports third-party integrations but is not designed for true white-labeling.
- **Target:** Large enterprise, government, financial services (500+ agents)
- **Pricing:** Custom enterprise — typically $200K–$2M+ annually
- **Strengths:** Deep compliance features (PII redaction, recording retention), strong in regulated industries (finance, healthcare, government), mature analytics and workforce optimization
- **Weaknesses:** Legacy architecture being modernized, slow to adopt generative AI, complex deployment, prohibitive pricing for mid-market
- **Our angle:** Modern AI-native alternative at a fraction of the cost. Verint's customers are locked into monolithic WFO suites; we offer standalone intelligence that plugs into any stack.

**CallMiner (Eureka)** — $$$$$
- **What they offer:** Omnichannel conversation analytics — voice, chat, email, social. Uses NLP, emotion detection, automated scoring, and AI-driven topic discovery. Claims to be "global leader in AI-powered conversation intelligence."
- **White-label/OEM:** No. Sold as CallMiner-branded platform only.
- **Target:** Large enterprise contact centers, compliance-heavy industries
- **Pricing:** Custom enterprise — $200K–$500K+ annually
- **Strengths:** Deepest omnichannel analytics (not just voice), strong compliance and QA automation, emotion detection beyond basic sentiment, Gartner-recognized leader
- **Weaknesses:** 6+ month implementation cycles, extremely expensive, no self-serve or API-first option, no white-label capability, focused exclusively on contact centers
- **Our angle:** 10x faster to deploy, API-first, white-label ready. CallMiner can't serve the partner/embedded channel at all.

**Calabrio (ONE Suite)** — $$$$
- **What they offer:** Workforce optimization suite with speech analytics, QA, sentiment analysis, and AI-powered workforce intelligence (launched 2025). Cloud-native, enterprise-grade.
- **White-label/OEM:** No direct white-label, but sold through CCaaS partnerships (available on RingCentral App Gallery, etc.)
- **Target:** Mid-market to enterprise contact centers (100–5,000 agents)
- **Pricing:** Custom — typically $50K–$300K annually
- **Strengths:** Strong WFO integration (scheduling + analytics in one), good CCaaS partnerships, more affordable than NICE/Verint
- **Weaknesses:** Analytics less deep than NICE/Verint/CallMiner, primarily a workforce tool with analytics bolted on, no standalone conversation intelligence API
- **Our angle:** We offer deeper AI insights (action items, coaching, follow-up drafts) without requiring a full WFO suite. Our white-label model means we can power the analytics layer for platforms like Calabrio.

#### Category B: AI-Native Contact Center Intelligence

These are modern, VC-backed companies purpose-built for AI-driven contact center optimization. They are our closest direct competitors.

**Observe.AI** — $$$
- **What they offer:** AI-powered contact center platform for real-time agent coaching, QA automation, and conversation analytics. $125M Series C funding.
- **White-label/OEM:** No. Branded platform only.
- **Target:** Mid-market to enterprise contact centers (100–5,000 agents)
- **Pricing:** Custom — estimated $50–$100/agent/month at scale
- **Strengths:** Purpose-built for contact centers, strong real-time coaching, growing fast, good enterprise traction
- **Weaknesses:** Contact-center only (no sales use case), no white-label or embedded option, no API-first mode for partners, limited to voice channel
- **Our angle:** We serve both sales AND support. We offer white-label and API-first modes that Observe.AI cannot. Partners who want embedded intelligence choose us.

**Cresta** — $$$$
- **What they offer:** Enterprise generative AI platform for contact centers — AI agents, real-time agent assist, and conversation intelligence. Roots in Google CCAI and OpenAI. Customers include United Airlines, Hilton, Square. Launched "Knowledge Agent" (March 2026) for real-time agent answers.
- **White-label/OEM:** No. Sold as Cresta-branded enterprise platform.
- **Target:** Large enterprise (1,000+ agents)
- **Pricing:** Custom enterprise — estimated $100K–$500K+ annually
- **Strengths:** Best-in-class real-time coaching, strong enterprise logos, proprietary models trained on customer data, deep no-code workflow customization
- **Weaknesses:** Enterprise-only (long sales cycles, high minimums), no mid-market play, no white-label, no API for embedding, expensive
- **Our angle:** We compete below Cresta's price floor. Our white-label model means telephony/CRM partners can offer Cresta-like features under their own brand without the enterprise price tag.

**Balto** — $$$
- **What they offer:** Real-time AI guidance for contact center agents — live coaching prompts, 100% QA coverage, call summarization. $57.4M total funding. Supports 20+ languages.
- **White-label/OEM:** No. Sold as Balto-branded platform.
- **Target:** Mid-market to enterprise contact centers
- **Pricing:** Custom — estimated $50–$100/agent/month
- **Strengths:** Strong real-time guidance (pops up coaching tips during live calls), 100% automated QA, good compliance audit trail, multi-language support
- **Weaknesses:** Focused narrowly on real-time guidance (not post-call analytics or action items), no white-label, no API-first model, no sales team use case
- **Our angle:** We offer the full lifecycle — real-time AND post-call intelligence, plus action item tracking and follow-up automation. Balto stops at coaching; we close the loop.

**Uniphore** — $$$$
- **What they offer:** Enterprise conversational AI platform — real-time analytics, agent assist, emotion/sentiment detection, and automation. $260M Series F (Oct 2025) backed by NVIDIA, Snowflake, and Databricks. Valued at $2.5B+ after prior Series E.
- **White-label/OEM:** Limited partner integrations but not a true white-label play.
- **Target:** Large enterprise (global banks, telecoms, insurers)
- **Pricing:** Custom enterprise — $200K+ annually
- **Strengths:** Massive funding ($700M+ total), strong in emotion AI beyond text sentiment, multimodal analysis (voice + video + text), deep enterprise relationships, global presence
- **Weaknesses:** Complex enterprise-only sales motion, heavy implementation, priced out of mid-market entirely, not embeddable or API-first
- **Our angle:** Uniphore targets the top 500 global enterprises. We serve the next 50,000 companies that want similar intelligence at 1/10th the cost, plus the partners who want to embed it.

#### Category C: Speech-to-Text API Providers (Build-Your-Own Layer)

These companies sell the transcription building block. They are both potential suppliers AND competitors — if they move up-stack into insights, they compete directly.

**Deepgram** — $$
- **What they offer:** Speech-to-text and text-to-speech APIs. Nova-3 model supports 45+ languages with diarization, smart formatting, topic detection. On-premise deployment available.
- **White-label/OEM:** Yes — API-only, partners build their own UI. On-premise available for enterprise.
- **Target:** Developers and enterprises building voice-powered applications
- **Pricing:** Pay-as-you-go from $0.0043/min (pre-recorded), $0.0077/min (streaming). Growth tier at $4K+/year for 20% discount. Enterprise custom pricing as low as $0.003/min at volume.
- **Strengths:** Best price/performance ratio in STT, fast and accurate, simple API, automatic volume discounts, on-premise option
- **Weaknesses:** Transcription only — no AI analysis, no action items, no coaching, no dashboard. Customers must build the entire intelligence layer themselves.
- **Competitive relationship:** Deepgram is a **supplier** (we can use their API) and a potential competitor if they move into insights. Currently they are infrastructure, not a product.

**AssemblyAI** — $$
- **What they offer:** Speech-to-text API with add-on intelligence features — speaker ID, sentiment analysis, PII redaction, summarization. Base rate $0.15/hour ($0.0025/min).
- **White-label/OEM:** Yes — API-only with no end-user branding.
- **Target:** Developers and SaaS companies building transcription features
- **Pricing:** Pay-as-you-go at $0.0025/min base, but add-ons stack fast — speaker ID (+$0.02/hr), sentiment (+$0.02/hr), PII redaction (+$0.08/hr), summarization (+$0.03/hr). Full-featured can 3–4x the base price. $50 free credits for new accounts.
- **Strengths:** Good developer experience, modular feature add-ons, growing LeMUR AI layer for generative insights
- **Weaknesses:** Intelligence features are shallow compared to purpose-built platforms, no dashboard/UI, no action item tracking, no coaching, add-on pricing gets expensive
- **Competitive relationship:** Potential **supplier** for transcription. Their LeMUR product is the closest to competing with our AI layer, but lacks workflow features (assignment, tracking, CRM sync).

**Symbl.ai** — $$
- **What they offer:** Conversation intelligence API platform with real-time and async analysis — topic extraction, action items, sentiment, questions detection. Proprietary "Nebula LLM" for streaming conversation understanding. Agentic AI framework for building autonomous conversational agents.
- **White-label/OEM:** Yes — explicitly designed for embedding. API-first, no Symbl branding on end-user experience.
- **Target:** Developers and SaaS companies building conversation intelligence into their products
- **Pricing:** Custom — contact sales
- **Strengths:** Most direct API competitor for our embedded/white-label use case, real-time streaming intelligence, proprietary LLM for conversations, developer-focused
- **Weaknesses:** Requires significant development effort to build a usable product, no turnkey dashboard, limited traction vs. Deepgram/AssemblyAI, smaller ecosystem
- **Competitive relationship:** **Most direct competitor** for the embedded/OEM channel. Symbl.ai sells building blocks; we sell a complete white-label product. Their customers still need to build UI, workflows, and integrations — we provide all of that out of the box.

**Rev.ai** — $
- **What they offer:** Speech-to-text API with async and streaming transcription. Lowest entry-level pricing in market at $0.002/min (Standard Model), $0.003/min (Reverb ASR).
- **White-label/OEM:** Yes — API-only.
- **Target:** Developers, media companies, SaaS builders
- **Pricing:** $0.002–$0.003/min base. Insights features (topic extraction, sentiment) priced separately. Enterprise volume discounts available but not published.
- **Strengths:** Cheapest STT on the market, backed by Rev.com's human transcription expertise
- **Weaknesses:** Transcription only, insights features are basic add-ons, no conversation intelligence or workflow layer
- **Competitive relationship:** Potential low-cost **supplier** for transcription. Not a product competitor.

**Speechmatics** — $$
- **What they offer:** Speech recognition API with strong multilingual support and on-premise deployment options. UK-based.
- **White-label/OEM:** Yes — API and on-premise. Strong in regulated industries needing data sovereignty.
- **Target:** Enterprise and government customers with compliance requirements
- **Pricing:** Custom enterprise pricing
- **Strengths:** Best-in-class multilingual accuracy, on-premise/air-gapped deployment, strong in EU/UK regulated markets
- **Weaknesses:** Transcription only, no intelligence layer, limited US market presence
- **Competitive relationship:** Potential **supplier** for multilingual/on-premise transcription needs.

#### Category D: Cloud Hyperscaler Offerings

The biggest long-term threat — AWS, Google, and Microsoft can bundle conversation intelligence into their cloud platforms at near-zero margin.

**AWS (Amazon Transcribe + Contact Lens for Amazon Connect)** — $$
- **What they offer:** Amazon Transcribe for STT ($0.024/min), Contact Lens for post-call and real-time analytics within Amazon Connect — sentiment, call summarization, category detection, PII redaction.
- **White-label/OEM:** Contact Lens is tied to Amazon Connect. Transcribe is a standalone API.
- **Target:** AWS-native enterprises using Amazon Connect
- **Pricing:** Transcribe at $0.024/min (expensive vs. Deepgram/AssemblyAI). Contact Lens bundled with Connect at $0.015/min.
- **Strengths:** AWS ecosystem lock-in, easy for existing Connect customers, integrated billing, global infrastructure
- **Weaknesses:** Contact Lens ONLY works with Amazon Connect (not other telephony), Transcribe accuracy trails Deepgram/Whisper, analytics are basic compared to NICE/Verint, no white-label
- **Our angle:** We are telephony-agnostic. AWS locks you into Connect. Our insights are far deeper than Contact Lens basics.

**Google (Contact Center AI / CCAI)** — $$$
- **What they offer:** CCAI Platform with real-time agent assist, virtual agents, conversation analytics, and Google's Speech-to-Text API. Deep integration with Google Cloud.
- **White-label/OEM:** CCAI Platform can be deployed by CCaaS partners (Genesys, NICE, etc. have CCAI integrations), but it's Google-branded infrastructure.
- **Target:** Enterprise contact centers on Google Cloud
- **Pricing:** Custom enterprise — Speech-to-Text starts at $0.016/min
- **Strengths:** Google's speech models, strong agent assist, virtual agent capabilities, CCAI partnerships with major CCaaS vendors
- **Weaknesses:** Tightly coupled to Google Cloud, complex to implement, analytics less mature than NICE/Verint, not a standalone product
- **Our angle:** We offer a complete product, not cloud infrastructure. No Google Cloud dependency. Faster to deploy with deeper AI insights.

**Microsoft (Dynamics 365 + Nuance)** — $$$$
- **What they offer:** Nuance acquisition ($19.7B) gives Microsoft enterprise speech AI, combined with Dynamics 365 Customer Service Insights and Copilot for Service. Azure Speech Services for STT.
- **White-label/OEM:** No. Sold as part of Microsoft/Dynamics ecosystem.
- **Target:** Microsoft-stack enterprises (Teams, Dynamics, Azure)
- **Pricing:** Bundled with Dynamics 365 licenses ($50–$195/user/month) plus Azure consumption
- **Strengths:** Deepest enterprise distribution (Teams + Dynamics + Azure), Nuance's decades of speech expertise, Copilot AI across the stack, massive R&D budget
- **Weaknesses:** Requires Microsoft ecosystem commitment, Nuance integration still in progress, analytics not best-in-class vs. pure-play vendors, no standalone offering
- **Our angle:** We are stack-agnostic. Microsoft only serves Microsoft shops. We serve Salesforce, HubSpot, and independent telephony customers that Microsoft can't reach.

#### Category E: CCaaS Platforms with Built-In AI

These are full contact center platforms that are adding AI features natively, potentially reducing demand for standalone tools.

**Genesys (Cloud CX)** — $$$$
- **What they offer:** Leading CCaaS platform with native AI — speech analytics, predictive engagement, agent assist, workforce optimization. Strong enterprise adoption of AI features driving company momentum in FY2026.
- **White-label/OEM:** Genesys partners can resell the platform, but AI features are Genesys-branded.
- **Pricing:** $75–$155/user/month depending on tier
- **Our angle:** Genesys AI is good enough for basic analytics but lacks the depth of purpose-built intelligence. We can power the analytics layer for Genesys competitors who need AI parity.

**Talkdesk** — $$$
- **What they offer:** CCaaS platform with AI — named Leader in G2 Contact Center and AI Agents (Winter 2026), Leader in 2025 Gartner CCaaS Magic Quadrant. CEO declared "CCaaS is dead" — pivoting to "Customer Experience Automation" (CXA).
- **White-label/OEM:** Talkdesk Express is offered through partners (e.g., Windstream Enterprise), indicating a partner-friendly model.
- **Pricing:** $75–$125/user/month
- **Our angle:** Talkdesk's AI is tied to their CCaaS. We offer standalone intelligence that any CCaaS — including Talkdesk competitors — can embed.

### 4.3 Competitive Differentiation Matrix

| Capability | CallSight | NICE/Verint/CallMiner | Observe.AI/Cresta/Balto | Symbl.ai | Deepgram/AssemblyAI | AWS/Google/MSFT |
|-----------|-----------|----------------------|------------------------|----------|--------------------|-----------------|
| White-label / rebrandable | **Yes** | No | No | Partial (API only) | No (API only) | No |
| Multi-tenant architecture | **Yes** | No | No | No | No | No |
| Custom domain support | **Yes** | No | No | No | No | No |
| Turnkey dashboard + UI | **Yes** | Yes | Yes | No | No | Limited |
| API-first / headless mode | **Yes** | Limited | Limited | **Yes** | **Yes** | Yes |
| AI action item extraction | **Yes** | Basic | Yes | Basic | No | Basic |
| Follow-up email drafts | **Yes** | No | No | No | No | No |
| Rep coaching / suggestions | **Yes** | Yes | **Yes** | No | No | Limited |
| Real-time transcription | Yes | Yes | Yes | Yes | **Yes** | Yes |
| Speaker diarization | Yes | Yes | Yes | Yes | **Yes** | Yes |
| Sentiment analysis | Yes | **Yes** | Yes | Yes | Add-on | Yes |
| CRM integrations | Yes | Limited | Yes | No | No | MSFT only |
| Telephony-agnostic | **Yes** | Partial | Yes | **Yes** | **Yes** | No (platform-locked) |
| Self-hosted / on-prem option | Yes | Yes | No | No | Yes (Deepgram) | No |
| Time to deploy | Days | 3–6+ months | 1–3 months | Weeks (dev required) | Hours (STT only) | Weeks–months |
| Minimum contract | None | $150K+/yr | $50K+/yr | Custom | $0 (pay-as-you-go) | Varies |
| Mid-market accessible | **Yes** | No | Partial | Yes | Yes | Partial |

### 4.4 Key Competitive Insight

The market has a clear gap:

- **Enterprise incumbents** (NICE, Verint, CallMiner) are too expensive, too slow to deploy, and cannot be white-labeled.
- **AI-native startups** (Observe.AI, Cresta, Balto) are modern but sell branded platforms — they don't enable partners.
- **API providers** (Deepgram, AssemblyAI, Symbl.ai) sell building blocks — customers must build the product themselves.
- **Hyperscalers** (AWS, Google, Microsoft) lock customers into their cloud ecosystem.

**CallSight occupies the only position that combines a turnkey product (dashboard + workflows + integrations) with true white-label multi-tenancy and API-first embeddability.** No existing player serves this niche.

---

## 5. SWOT Analysis

### Strengths
- **Only turnkey white-label product in market** — NICE/Verint/CallMiner can't be white-labeled; Symbl.ai/Deepgram are raw APIs with no UI. We are the only complete product that partners can rebrand and resell.
- **AI-native architecture** — built around Claude's reasoning capabilities rather than retrofitting generative AI onto legacy speech analytics platforms (as NICE, Verint, and CallMiner are doing)
- **Dual-channel GTM** — direct sales to mid-market teams AND white-label to CPaaS/CRM/BPO partners, creating two independent revenue streams with different risk profiles
- **Telephony-agnostic** — works with any phone system, unlike AWS Contact Lens (Connect-only), Google CCAI (GCP-only), or Microsoft (Dynamics-only)
- **Speed-to-deploy** — days vs. 3–6 months for NICE/Verint/CallMiner, giving us a structural advantage in mid-market sales cycles
- **Pluggable ASR engines** — not locked to one transcription vendor; can use Deepgram for speed, Whisper for cost, or AssemblyAI for features, optimizing cost/quality per customer
- **API-first + turnkey** — serves both developers who want headless API access AND business users who want a dashboard, unlike competitors that offer only one

### Weaknesses
- **No brand recognition** — competing against Uniphore ($700M+ raised, $2.5B valuation), Cresta (backed by Google/OpenAI alumni), and Observe.AI ($125M Series C). Enterprise buyers default to known names.
- **No proprietary speech model** — using third-party ASR (Deepgram, Whisper) means no moat at the transcription layer. Competitors like NICE (30 years of CX data) and Deepgram (custom-trained Nova models) have proprietary advantages.
- **Dependency on third-party AI APIs** — Claude API costs and availability are outside our control. A significant pricing change could erode unit economics.
- **Small team vs. incumbents with thousands of employees** — limited capacity for enterprise sales cycles, custom integrations, 24/7 support, and compliance certifications that enterprise buyers require
- **Multi-tenant complexity** — white-label architecture with tenant isolation adds engineering overhead, security attack surface, and operational complexity vs. single-tenant competitors
- **No existing customer data flywheel** — NICE/Verint/CallMiner have billions of analyzed calls improving their models. We start from zero.

### Opportunities
- **$40B+ TAM by 2030** — conversation intelligence is one of the fastest-growing enterprise AI categories, and the embedded/white-label subsegment is almost entirely unaddressed
- **Massive underserved partner channel** — thousands of CPaaS providers, regional telecoms, vertical CRM vendors, and BPOs want to offer AI call intelligence but will never build it themselves. No incumbent serves them.
- **Enterprise incumbents are slow to modernize** — NICE, Verint, and CallMiner are retrofitting generative AI onto architectures built in the 2000s–2010s. There is a window to offer a modern alternative before they catch up.
- **CCaaS platforms need AI parity** — smaller CCaaS vendors (Five9, 8x8, Vonage, regional players) need AI analytics to compete with Genesys and Talkdesk but can't build it. We can be their AI layer.
- **Regulatory tailwinds** — financial services (MiFID II, Dodd-Frank), healthcare (HIPAA), and insurance industries face increasing mandates for call recording, analysis, and compliance monitoring
- **API providers leaving money on the table** — Deepgram, AssemblyAI, and Rev.ai sell transcription at $0.002–0.007/min. By building the intelligence layer on top, we can charge $0.05–0.15/call — 10–50x margin on the same underlying audio.
- **International expansion** — Whisper supports 90+ languages and Deepgram supports 45+. Most enterprise competitors (Cresta, Balto, Observe.AI) are English-first.

### Threats
- **Deepgram or AssemblyAI move up-stack** — both are well-funded and could build a turnkey product + white-label offering on top of their existing STT APIs, directly competing in our niche
- **Symbl.ai wins the embedded race** — Symbl.ai is the closest API competitor for the OEM channel. If they ship a turnkey dashboard layer, they become a direct threat.
- **Hyperscaler bundling** — AWS, Google, or Microsoft could bundle conversation intelligence into their cloud platforms at cost, collapsing the market for standalone vendors
- **NICE/Verint launch mid-market tiers** — enterprise incumbents could create self-serve or lower-cost offerings to address the mid-market gap we're targeting
- **Uniphore/Cresta expand into white-label** — with $700M+ and $200M+ in funding respectively, either could pivot to an embedded/partner model
- **AI API pricing volatility** — significant increases in Claude API pricing could erode unit economics; must maintain multi-model optionality (Claude + Gemini + open-source fallback)
- **Privacy regulation tightens** — stricter call recording consent laws (especially GDPR enforcement, US state-level laws) could limit addressable market or increase compliance costs
- **Open-source convergence** — Whisper + open LLMs (Llama, Mistral) + open-source diarization could enable a fully self-hosted alternative, commoditizing the entire stack

---

## 6. Business Model & Pricing

### 6.1 Direct Sales Pricing

| Plan | Price | Included | Target |
|------|-------|----------|--------|
| **Starter** | $0/mo | 5 calls/month, basic transcription, AI summary only | Evaluation / solo users |
| **Professional** | $49/seat/mo | Unlimited calls, full AI analysis, action items, 2 integrations | Small teams (5–20 reps) |
| **Business** | $89/seat/mo | Everything in Pro + coaching, analytics, priority support, custom branding | Mid-market (20–200 reps) |
| **Enterprise** | Custom | Dedicated instance, SSO/SAML, SLA, custom integrations, audit logs | Large orgs (200+ reps) |

**Usage add-ons:**
- Additional transcription minutes: $0.02/min beyond plan limit
- Premium AI (Opus-tier analysis): $0.10/call
- Additional storage: $5/100GB/month

### 6.2 White-Label Partner Pricing

| Model | Structure | Details |
|-------|-----------|---------|
| **Revenue Share** | 70/30 split (partner keeps 70%) | Partner sets end-user pricing, we provide infrastructure |
| **Platform License** | $2,000–$10,000/mo base + $0.03/min usage | Fixed monthly fee + per-minute transcription |
| **OEM / Embedded** | $0.05–$0.15/call API-only | Headless API, partner builds their own UI |

### 6.3 Unit Economics (Target at Scale)

| Metric | Target |
|--------|--------|
| Gross margin | 75–80% |
| CAC (direct) | $800–$1,200 |
| CAC (partner channel) | $200–$400 |
| LTV (professional seat) | $2,400 (avg 48-month lifespan) |
| LTV:CAC ratio | 3:1 (direct), 6:1+ (partner) |
| Monthly churn target | < 3% (logo), < 1% (revenue, net of expansion) |
| Payback period | < 12 months |

---

## 7. Go-To-Market Strategy

### 7.1 Phase 1 — Product-Led Growth (Months 1–6)

**Goal:** 500 free users, 50 paying customers, validate product-market fit

- Launch free tier with generous limits (5 calls/month)
- Content marketing: SEO-optimized blog posts targeting "call transcription software", "AI sales coaching", "call center analytics"
- Developer marketing: API docs, SDKs, developer blog, Hacker News launch
- Product Hunt launch
- Community: Discord/Slack community for users and partners
- Social proof: Case studies from design partners (recruit 5–10 pre-launch)

**Channels:**
| Channel | Activity | Goal |
|---------|----------|------|
| Organic search | 2 blog posts/week, landing pages per use case | 10K monthly visitors by month 6 |
| Product Hunt | Launch + follow-up features | 500 signups |
| LinkedIn | Founder thought leadership, sales tips content | 5K followers |
| Developer community | API tutorials, open-source contributions | 200 API users |

### 7.2 Phase 2 — Sales-Assisted Growth (Months 6–12)

**Goal:** $500K ARR, 10 white-label partners in pipeline

- Hire 2 AEs focused on mid-market direct sales
- Hire 1 Partner Manager focused on white-label deals
- Outbound prospecting to sales and support leaders at target companies
- Conference presence: SaaStr, Customer Contact Week, Twilio SIGNAL
- Partner program launch: Telephony providers, CRM vendors, BPOs
- Webinar series: "AI-Powered Sales Coaching" with industry guests

### 7.3 Phase 3 — Channel Acceleration (Months 12–24)

**Goal:** $3M ARR, 5 live white-label partners, 50 in pipeline

- Scale partner program with dedicated enablement team
- Co-marketing with partners (joint webinars, case studies, co-branded content)
- Launch marketplace listings (Salesforce AppExchange, HubSpot Marketplace, Twilio Marketplace)
- Expand to vertical-specific solutions (healthcare, financial services, real estate)
- International expansion (start with UK, DACH, ANZ)

### 7.4 Marketing Mix

| Strategy | Budget Allocation | Expected CAC Impact |
|----------|------------------|-------------------|
| Content / SEO | 25% | Low CAC, long payback |
| Paid search (Google Ads) | 20% | Medium CAC, immediate |
| LinkedIn advertising | 15% | Medium CAC, targeted |
| Events / conferences | 15% | High CAC, high deal size |
| Partner co-marketing | 15% | Lowest CAC |
| Developer relations | 10% | Low CAC, long payback |

---

## 8. Sales Strategy

### 8.1 Direct Sales Motion

```
Inbound lead (free signup / demo request)
    → Automated onboarding email sequence (7 days)
    → Product-qualified lead (PQL) trigger: uploaded 3+ calls
    → SDR outreach: personalized based on usage data
    → AE demo: show insights from their actual calls
    → Trial: 14-day full-feature Business trial
    → Close: annual contract with monthly billing option
```

**Key sales assets:**
- **ROI calculator:** Input team size + call volume → projected time savings and revenue impact
- **Live demo environment:** Pre-loaded with sample calls showing full AI analysis
- **Competitive battle cards:** Gong vs. CallSight, Fireflies vs. CallSight
- **Security whitepaper:** SOC 2, encryption, data residency details

### 8.2 White-Label Sales Motion

```
Partner identification (telephony/CRM vendor, BPO)
    → Technical discovery: API requirements, branding needs, volume
    → POC: Deploy white-labeled instance with partner branding
    → Integration sprint: Connect to partner's telephony/CRM
    → Pilot: 30-day pilot with partner's customers
    → Contract: Revenue share or platform license agreement
    → Launch: Co-marketing announcement, enablement training
```

---

## 9. Key Metrics & KPIs

### 9.1 Product Metrics
| Metric | Definition | Target (Year 1) |
|--------|-----------|-----------------|
| Calls processed/month | Total calls transcribed + analyzed | 50K by month 12 |
| Transcription accuracy | Word error rate (WER) | < 8% |
| AI action item acceptance rate | % of AI-suggested actions marked "useful" | > 70% |
| Time-to-insight | Upload to AI analysis complete | < 5 min (avg) |
| Daily active users (DAU) | Users who log in and view a call/insight | 30% of paid seats |

### 9.2 Business Metrics
| Metric | Target (Year 1) | Target (Year 2) |
|--------|-----------------|-----------------|
| ARR | $500K | $3M |
| Paying customers | 100 | 400 |
| White-label partners (live) | 2 | 10 |
| Net revenue retention | > 110% | > 120% |
| Gross margin | > 70% | > 78% |
| Monthly burn rate | < $80K | < $150K |

### 9.3 Marketing Metrics
| Metric | Target (Year 1) |
|--------|-----------------|
| Website monthly visitors | 25K |
| Free-to-paid conversion | 5–8% |
| Demo-to-close rate | 25% |
| Organic traffic share | > 40% |
| Content pieces published | 100+ |

---

## 10. Financial Projections (3-Year Summary)

| | Year 1 | Year 2 | Year 3 |
|---|--------|--------|--------|
| **Revenue** | $500K | $3M | $12M |
| Direct sales | $350K | $1.8M | $6M |
| White-label / partner | $150K | $1.2M | $6M |
| **COGS** (infra + AI API) | $125K | $660K | $2.4M |
| **Gross profit** | $375K | $2.34M | $9.6M |
| **Gross margin** | 75% | 78% | 80% |
| **Operating expenses** | $960K | $2.1M | $5.5M |
| Engineering (team of 4→8→15) | $480K | $1.05M | $2.8M |
| Sales & marketing | $300K | $700K | $1.8M |
| G&A | $180K | $350K | $900K |
| **Net income (loss)** | ($585K) | $240K | $4.1M |
| **Headcount** | 8 | 18 | 35 |

**Key assumptions:**
- Average direct deal size: $3,500/month by year 2
- White-label deals average $8,000/month by year 2
- AI API costs decrease 15–20% annually as model efficiency improves
- 75% of revenue on annual contracts by year 2

---

## 11. Risk Mitigation

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Gong launches SMB tier | High | Medium | Differentiate on white-label, speed to value, and pricing transparency |
| Claude API outage / price hike | High | Low | Multi-model support (Claude + Gemini + open-source fallback), negotiate volume pricing |
| Slow white-label sales cycle | Medium | High | Build direct revenue first, use partner pipeline as upside not dependency |
| Data breach / security incident | Critical | Low | SOC 2 from day one, pen testing, bug bounty, encryption at rest/transit |
| High churn in first year | High | Medium | Invest in onboarding, ensure time-to-value < 24 hours, NPS surveys, success team |
| Regulatory changes (recording consent) | Medium | Medium | Built-in consent workflows, data residency options, legal review per market |

---

## 12. Team Requirements (Initial)

| Role | Count | Priority | Rationale |
|------|-------|----------|-----------|
| Founding Engineer (Backend/AI) | 1 | Critical | Core pipeline: transcription, AI analysis, API |
| Founding Engineer (Full-Stack) | 1 | Critical | Dashboard, white-label theming, integrations |
| Founding Engineer (Infra/DevOps) | 1 | High | Multi-tenant infrastructure, CI/CD, security |
| Product Designer | 1 | High | UX for dashboard, onboarding, white-label system |
| Head of Sales | 1 | High | Direct sales motion + partner development |
| Founding Engineer (Frontend) | 1 | Medium | Scale frontend: analytics, embeddable widget |
| Customer Success | 1 | Medium | Onboarding, retention, feedback loop |
| Content Marketer | 1 | Medium | SEO, blog, case studies, social |
