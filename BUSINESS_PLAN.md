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

### 4.1 Competitive Landscape Map

```
                        HIGH PRICE
                            │
              Gong           │         Observe.AI
              Chorus (ZoomInfo)│         CallMiner
                            │
    ENTERPRISE ─────────────┼─────────────── AI-NATIVE
                            │
              Dialpad AI    │       ★ CallSight AI ★
              RingCentral   │         Fireflies.ai
                            │         Otter.ai
                            │
                        LOW PRICE
```

### 4.2 Competitor Deep-Dive

#### Gong ($$$$ — Enterprise)
- **Strengths:** Market leader, deep CRM integration, deal intelligence, massive training data
- **Weaknesses:** $100–$150/user/month minimum, requires 50+ seat commitment, no white-label, 12-month contracts, heavy onboarding
- **Our angle:** 70% of Gong's value at 30% of the cost. No seat minimums. White-label available.

#### Chorus (ZoomInfo) ($$$$ — Enterprise)
- **Strengths:** Bundled with ZoomInfo data, strong in outbound sales
- **Weaknesses:** Acquired by ZoomInfo — product investment slowing, lock-in to ZoomInfo ecosystem, no standalone option for support teams
- **Our angle:** Independent platform, works with any data provider, support + sales in one tool.

#### Fireflies.ai ($$ — SMB/Mid-Market)
- **Strengths:** Easy setup, meeting bot joins Zoom/Teams, generous free tier, good transcription
- **Weaknesses:** Focused on meetings (not phone calls), shallow AI analysis, no white-label, limited CRM integrations, no diarization for phone audio
- **Our angle:** Purpose-built for phone/VoIP calls with deep telephony integrations. Structured action items, not just summaries. White-label ready.

#### Otter.ai ($ — Consumer/SMB)
- **Strengths:** Great transcription quality, consumer brand recognition, low price
- **Weaknesses:** Consumer-focused, no sales/support workflows, no action item tracking, no multi-tenant, no API for embedding
- **Our angle:** Enterprise workflows, multi-tenant, API-first, white-label.

#### Observe.AI ($$$ — Contact Center)
- **Strengths:** Purpose-built for contact centers, agent coaching, QA automation
- **Weaknesses:** Contact-center only, complex implementation, expensive, no sales use case
- **Our angle:** Unified sales + support, faster deployment, white-label for partners.

#### CallMiner ($$$$$ — Enterprise Contact Center)
- **Strengths:** Deep analytics, compliance features, mature platform
- **Weaknesses:** Legacy architecture, 6+ month implementation, $200K+ annual contracts
- **Our angle:** Modern stack, API-first, deploy in days not months, 10x cheaper.

### 4.3 Competitive Differentiation Matrix

| Capability | CallSight | Gong | Fireflies | Otter | Observe.AI |
|-----------|-----------|------|-----------|-------|------------|
| Phone call transcription | Yes | Yes | Limited | No | Yes |
| Meeting transcription | Yes | Yes | Yes | Yes | No |
| Speaker diarization | Yes | Yes | Basic | Yes | Yes |
| AI action items | Yes | Yes | Basic | No | Yes |
| Follow-up email drafts | Yes | No | No | No | No |
| Rep coaching | Yes | Yes | No | No | Yes |
| White-label / embeddable | **Yes** | No | No | No | No |
| Multi-tenant architecture | **Yes** | No | No | No | No |
| Custom domain support | **Yes** | No | No | No | No |
| API-first / headless mode | **Yes** | Limited | Yes | Limited | Limited |
| Self-hosted option | **Yes** | No | No | No | No |
| Seat minimums | None | 50+ | None | None | 100+ |
| Free tier | Yes | No | Yes | Yes | No |
| CRM integration | Yes | Deep | Basic | No | Yes |
| Real-time transcription | Yes | Yes | Yes | Yes | Yes |

---

## 5. SWOT Analysis

### Strengths
- **White-label architecture from day one** — no competitor offers true multi-tenant white-labeling, creating a defensible channel strategy
- **AI-native platform** — built around Claude's reasoning capabilities rather than retrofitting AI onto legacy transcription
- **Unified sales + support** — single platform for both use cases vs. competitors that specialize in one
- **API-first design** — enables embedding into any workflow, unlocking the partner/platform channel
- **Modern tech stack** — faster iteration, lower infrastructure costs, easier to hire for
- **No seat minimums** — accessible to teams of any size, reducing sales friction
- **Pluggable ASR engines** — not locked to one transcription vendor; can optimize cost/quality per customer

### Weaknesses
- **No brand recognition** — entering a market with well-funded incumbents (Gong raised $584M)
- **No existing training data** — competitors have millions of calls to improve models; we start cold
- **Dependency on third-party AI** — Claude API costs and availability are outside our control
- **Small team initially** — limited capacity for enterprise sales cycles, custom integrations, and 24/7 support
- **Transcription is not proprietary** — using open-source/third-party ASR means no moat at the transcription layer
- **White-label complexity** — multi-tenant architecture adds engineering overhead and potential attack surface

### Opportunities
- **$40B+ TAM by 2030** — conversation intelligence is one of the fastest-growing enterprise AI categories
- **White-label channel is wide open** — thousands of telephony providers, CRM vendors, and BPOs want AI features but won't build them
- **Mid-market is underserved** — Gong/Chorus price out smaller teams; Fireflies/Otter lack enterprise features
- **AI quality leap** — Claude's reasoning capabilities enable qualitatively better coaching and action items vs. GPT-3.5 era competitors
- **Regulatory tailwinds** — industries like financial services and healthcare face compliance mandates for call recording and analysis
- **International expansion** — Whisper supports 90+ languages; most competitors are English-first
- **Platform play** — once embedded via white-label, switching costs are extremely high for partners

### Threats
- **Gong moves downmarket** — Gong could launch a self-serve SMB tier at lower price points
- **CRM giants build native** — Salesforce (Einstein), HubSpot, or Zoom could build equivalent features natively
- **ASR commoditization** — as transcription becomes a commodity, margins compress on the transcription layer
- **AI API pricing changes** — significant Claude API price increases could erode unit economics
- **Privacy regulation** — stricter call recording consent laws (especially EU) could limit addressable market
- **Open-source alternatives** — community-built solutions combining Whisper + open LLMs could undercut pricing
- **Enterprise procurement cycles** — long sales cycles for white-label deals could strain cash flow

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
