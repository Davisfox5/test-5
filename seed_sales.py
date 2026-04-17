#!/usr/bin/env python3
"""
CallSight AI — Sales Call Transcript Seed Script
Creates schema and populates 18 realistic sales call transcripts.
No AI analysis is run here — transcripts only.
"""

import os
import uuid
import json
import random
import psycopg2
from datetime import datetime, timedelta
# Load .env manually since dotenv may not be installed
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

DB_URL = os.environ["DATABASE_URL"]

# ── Helpers ──────────────────────────────────────────────────────────────────

def new_id():
    return str(uuid.uuid4())

def ms_from_words(text, start_ms):
    """Estimate end_ms from word count at ~140 wpm."""
    words = len(text.split())
    duration_ms = int((words / 140) * 60 * 1000)
    return start_ms + max(duration_ms, 1500)

def build_full_text(segments):
    return " ".join(f"{s['speaker_name']}: {s['text']}" for s in segments)

def days_ago(n):
    return datetime.utcnow() - timedelta(days=n)

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
DROP TABLE IF EXISTS action_items, ai_insights, transcript_segments,
                     transcripts, calls, users, tenants CASCADE;

CREATE TABLE tenants (
    id              UUID PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    slug            VARCHAR(63)  UNIQUE NOT NULL,
    plan            VARCHAR(50)  DEFAULT 'enterprise',
    branding_config JSONB        DEFAULT '{}',
    settings        JSONB        DEFAULT '{}',
    created_at      TIMESTAMPTZ  DEFAULT now(),
    updated_at      TIMESTAMPTZ  DEFAULT now()
);

CREATE TABLE users (
    id          UUID PRIMARY KEY,
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    email       VARCHAR(255) NOT NULL,
    full_name   VARCHAR(255),
    role        VARCHAR(50)  DEFAULT 'member',
    department  VARCHAR(100),
    is_active   BOOLEAN      DEFAULT TRUE,
    created_at  TIMESTAMPTZ  DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE TABLE calls (
    id            UUID PRIMARY KEY,
    tenant_id     UUID REFERENCES tenants(id) ON DELETE CASCADE,
    uploaded_by   UUID REFERENCES users(id),
    title         VARCHAR(500),
    call_type     VARCHAR(50),
    participants  JSONB        DEFAULT '[]',
    duration_secs INTEGER,
    audio_url     TEXT         NOT NULL DEFAULT '',
    audio_format  VARCHAR(20)  DEFAULT 'mp3',
    status        VARCHAR(50)  DEFAULT 'complete',
    source        VARCHAR(100),
    metadata      JSONB        DEFAULT '{}',
    created_at    TIMESTAMPTZ  DEFAULT now(),
    updated_at    TIMESTAMPTZ  DEFAULT now()
);

CREATE TABLE transcripts (
    id         UUID PRIMARY KEY,
    call_id    UUID REFERENCES calls(id) ON DELETE CASCADE,
    tenant_id  UUID REFERENCES tenants(id) ON DELETE CASCADE,
    language   VARCHAR(10) DEFAULT 'en',
    full_text  TEXT,
    word_count INTEGER,
    confidence FLOAT       DEFAULT 0.97,
    engine     VARCHAR(50) DEFAULT 'whisper',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE transcript_segments (
    id            UUID PRIMARY KEY,
    transcript_id UUID REFERENCES transcripts(id) ON DELETE CASCADE,
    speaker_id    VARCHAR(100),
    speaker_name  VARCHAR(255),
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    text          TEXT    NOT NULL,
    confidence    FLOAT   DEFAULT 0.97,
    sentiment     VARCHAR(20),
    seq_order     INTEGER NOT NULL
);

CREATE TABLE ai_insights (
    id               UUID PRIMARY KEY,
    call_id          UUID REFERENCES calls(id) ON DELETE CASCADE,
    tenant_id        UUID REFERENCES tenants(id) ON DELETE CASCADE,
    model            VARCHAR(100) DEFAULT 'claude-sonnet-4-6',
    summary          TEXT,
    sentiment_overall VARCHAR(20),
    sentiment_score  FLOAT,
    topics           JSONB DEFAULT '[]',
    key_moments      JSONB DEFAULT '[]',
    coaching         JSONB DEFAULT '{}',
    raw_response     JSONB,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE action_items (
    id           UUID PRIMARY KEY,
    insight_id   UUID REFERENCES ai_insights(id) ON DELETE CASCADE,
    call_id      UUID REFERENCES calls(id) ON DELETE CASCADE,
    tenant_id    UUID REFERENCES tenants(id) ON DELETE CASCADE,
    assigned_to  UUID REFERENCES users(id),
    title        VARCHAR(500) NOT NULL,
    description  TEXT,
    priority     VARCHAR(20)  DEFAULT 'medium',
    due_date     DATE,
    status       VARCHAR(50)  DEFAULT 'pending',
    category     VARCHAR(100),
    created_at   TIMESTAMPTZ  DEFAULT now(),
    updated_at   TIMESTAMPTZ  DEFAULT now()
);

CREATE INDEX idx_calls_tenant   ON calls(tenant_id, created_at DESC);
CREATE INDEX idx_calls_type     ON calls(tenant_id, call_type);
CREATE INDEX idx_segs_transcript ON transcript_segments(transcript_id, seq_order);
"""

# ── Agent definitions ─────────────────────────────────────────────────────────

AGENTS = [
    {"full_name": "Marcus Webb",   "email": "marcus.webb@callsight.ai",   "role": "manager",  "department": "Sales"},
    {"full_name": "Priya Sharma",  "email": "priya.sharma@callsight.ai",  "role": "member",   "department": "Sales"},
    {"full_name": "Jordan Cole",   "email": "jordan.cole@callsight.ai",   "role": "member",   "department": "Sales"},
    {"full_name": "Aisha Thompson","email": "aisha.thompson@callsight.ai","role": "manager",  "department": "Customer Success"},
    {"full_name": "Derek Liu",     "email": "derek.liu@callsight.ai",     "role": "member",   "department": "Customer Success"},
    {"full_name": "Sam Patel",     "email": "sam.patel@callsight.ai",     "role": "member",   "department": "IT Support"},
    {"full_name": "Casey Rivera",  "email": "casey.rivera@callsight.ai",  "role": "member",   "department": "IT Support"},
]

# ── Sales call data ───────────────────────────────────────────────────────────

SALES_CALLS = [

    {
        "title": "Discovery — Apex Communications",
        "agent": "marcus.webb@callsight.ai",
        "customer_name": "Kevin Okafor",
        "customer_company": "Apex Communications",
        "customer_title": "VP of Product",
        "duration_secs": 1620,
        "source": "zoom",
        "days_ago": 3,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Well, kevin, really appreciate you making time today. I saw your post on LinkedIn about Apex looking to differentiate the platform — that's exactly the conversation I was hoping to have with you."},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor", "sentiment": "positive",
             "text": "I mean, yeah, we've been fielding a lot of requests from enterprise customers asking for call analytics. Right now we're delivering the pipes but they want the intelligence layer so on top of it. We've been evaluating a few options."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "That's the exact fit for what we built. CallSight is designed to be embedded into platforms like yours — you white-label it completely, your customers see your brand, and you're offering AI call intelligence without having to build or maintain any of the underlying infrastructure. What does your current uh analytics offering look like?"},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor", "sentiment": "neutral",
             "text": "Honestly, pretty thin. We have basic CDRs — call duration, volume, some routing data. Nothing on transcription, sentiment, any of the AI layer. We tried to build something internal about six months ago and it stalled out after the first sprint."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Right, so, that's extremely common for telephony providers. Transcription and NLP are a completely different engineering discipline. Can you tell me about your customer base — what industries are they primarily in?"},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor", "sentiment": "neutral",
             "text": "Mostly mid-market financial services and insurance. Companies with 50 to 500 agents. They're very compliance-conscious — FINRA, SOC 2, that kind of thing. That's actually been one of our blockers with vendors so far."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "That's actually good news for us. The architecture is built with PII redaction, tenant-level data isolation, and we're designed to operate in compliance-sensitive environments. Financial services is one of our — well, my team's primary target verticals. I'd love to show you the white-label dashboard — I can swap your logo and brand colors in literally thirty seconds during a demo."},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor", "sentiment": "positive",
             "text": "Alright, so, our CEO is going to want I mean to see that it looks native. That's a hard requirement — it  — sorry, can you hear me okay? —  cannot look like a third-party integration. If it has someone else's name anywhere on it, it's a non-starter."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Right, right. Completely understood, and that's exactly how it works. Zero CallSight branding visible to your end users. Your domain, your color system, your logo. And on the API side, the endpoints are fully documented — your team can call them directly from within your existing platform. I'd suggest we get your product and engineering teams on a deeper technical session this week or next. Would that work?"},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor", "sentiment": "positive",
             "text": "Yeah, let's do it. I'll pull in our Head of Platform Engineering. He's the one who'll ultimately have to sign off on the integration approach anyway."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Perfect. I'll send over a scheduling link and the technical spec sheet so he can review the API architecture ahead of the call. Really glad we connected on this, Kevin — I think the fit's genuinely strong here."},
        ],
    },

    {
        "title": "Product Demo — Riverside Financial Group",
        "agent": "priya.sharma@callsight.ai",
        "customer_name": "Sandra Bello",
        "customer_company": "Riverside Financial Group",
        "customer_title": "VP of Sales Operations",
        "duration_secs": 2880,
        "source": "zoom",
        "days_ago": 7,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Sandra, thanks for joining. I'm gonna share my screen and walk you through the dashboard, then I want to make sure we've time to talk through what a pilot would look like for your team specifically."},
            {"speaker_id": "customer", "speaker_name": "Sandra Bello", "sentiment": "positive",
             "text": "Sounds great. We have about 40 reps across two offices and the big pain right now is that managers have no visibility into what's actually happening on calls. We're flying blind on coaching."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Okay, okay. That's one of the core problems we solve. So what you're looking at right now is the overview dashboard. These four cards are your key metrics — total calls analyzed, average sentiment score, open action items, and team coaching score. Everything updates in near real-time after a call ends... does that make sense?"},
            {"speaker_id": "customer", "speaker_name": "Sandra Bello", "sentiment": "positive",
             "text": "How quickly does a call get processed after it ends? Our reps need to log activities in Salesforce within a couple hours."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Batch processing is typically under five minutes for a standard call. If you're using real-time mode via Twilio or Zoom integration, the transcript and a preliminary summary are available before the rep even hangs up. And on the Salesforce side, we push the summary, action items, and sentiment score directly into the activity record automatically."},
            {"speaker_id": "customer", "speaker_name": "Sandra Bello", "sentiment": "positive",
             "text": "That would honestly save our reps 20 minutes per call on CRM entry alone. Can I see what that Salesforce record actually looks like?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Absolutely — let me pull up a sample. You can see here the call summary populates in the description field, and action items become tasks automatically assigned to the rep with due dates. The field mapping is fully configurable, so if you use custom fields in your Salesforce org we can map to those."},
            {"speaker_id": "customer", "speaker_name": "Sandra Bello", "sentiment": "positive",
             "text": "We do have a lot of custom fields. That's actually a concern — we've been burned before so by integrations that only work with standard Salesforce objects."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "The integration uses the Salesforce REST API directly, so custom objects and fields are fully supported. During our preview engagement we actually sit down with your admin and configure the mapping together. It's part of the process."},
            {"speaker_id": "customer", "speaker_name": "Sandra Bello", "sentiment": "positive",
             "text": "I like that. Tell me about the coaching side — can I see what a manager actually sees for a specific rep?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Right, right. Yes — this is the team performance view. You can see individual rep scores over time, drill into specific calls, and see the AI-generated coaching notes which flag things like talk-to-listen ratio, use of filler words, objection handling patterns, and whether the rep committed to clear next steps. Managers find this really changes how their 1-on-1s work — they're talking about specific moments in specific calls instead of general impressions."},
            {"speaker_id": "customer", "speaker_name": "Sandra Bello", "sentiment": "positive",
             "text": "This is exactly what I've been trying to get my leadership to invest in. How do we move forward? I wanna get a proposal in front of my CRO before end of month."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Yeah. Let's do this — I'll send you a one-pager you can share with your CRO that focuses on the ROI angle. Then we schedule a 30-minute executive call where I walk them through the business case. From there we can scope the preview engagement around your specific team. Does that work?"},
            {"speaker_id": "customer", "speaker_name": "Sandra Bello", "sentiment": "positive",
             "text": "That works perfectly. Send that over today if you can — he's got a budget meeting Thursday and I'd love to get it in front of him beforehand. [chuckles] "},
        ],
    },

    {
        "title": "Cold Outreach — NovaCare Health Systems",
        "agent": "jordan.cole@callsight.ai",
        "customer_name": "Tom Haverford",
        "customer_company": "NovaCare Health Systems",
        "customer_title": "IT Director",
        "duration_secs": 780,
        "source": "phone",
        "days_ago": 14,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "Tom, thanks for picking up. Jordan Cole from CallSight AI — we build call transcription and AI analysis tools for healthcare organizations. I know this is out of nowhere, but I wanted to see if you had two minutes."},
            {"speaker_id": "customer", "speaker_name": "Tom Haverford",  "sentiment": "neutral",
             "text": "Sure, go ahead. Two minutes."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "Okay, so, appreciate it. NovaCare runs a patient services call center — we work with a few health systems of similar size and the big issue we keep hearing is that managers have no scalable way to review call quality or coach agents. Everything manual, sampling maybe five percent of calls. Does that resonate at all with what you're seeing?"},
            {"speaker_id": "customer", "speaker_name": "Tom Haverford",  "sentiment": "neutral",
             "text": "It does, honestly. But before we go any further — healthcare is heavily regulated. PHI, HIPAA, the whole stack. Every vendor we talk to says they're compliant but the devil like is always in the details."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "Okay, so, that's a completely fair concern and I wanna address it directly rather than gloss over it. We have PII and PHI redaction built into the transcription pipeline. Data is tenant-isolated, encrypted at rest and in transit, and we're happy to sign a BAA. I can get you our security documentation and architecture overview today."},
            {"speaker_id": "customer", "speaker_name": "Tom Haverford",  "sentiment": "neutral",
             "text": "A BAA is mandatory for us, not optional. And we'd need to involve our compliance officer before any data touches a third-party system... does that make sense?"},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "Makes sense. Okay, so, understood — that's exactly the right process and we're set up for it. What would be most helpful is if I sent you the security and compliance package and you looped in your compliance officer for a technical review call. No sales pressure, just a documentation review conversation. Would that be a reasonable next step?"},
            {"speaker_id": "customer", "speaker_name": "Tom Haverford",  "sentiment": "neutral",
             "text": "Send the documentation. If it checks out I can see about getting Sarah — our compliance officer — on a call. No promises on timing though."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "Right, right. Totally fair. I'll send that over in the next hour. And Tom — I appreciate the honest conversation. Most vendors dance around the HIPAA question. We'd rather address it head-on... does that make sense?"},
        ],
    },

    {
        "title": "Proposal Review — Summit BPO Partners",
        "agent": "marcus.webb@callsight.ai",
        "customer_name": "Rachel Chen",
        "customer_company": "Summit BPO Partners",
        "customer_title": "COO",
        "duration_secs": 3240,
        "source": "zoom",
        "days_ago": 5,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Rachel, thanks for coming back with the full team. I want to make sure we use this time to walk through the proposal and address anything that came up when you reviewed it internally."},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "positive",
             "text": "Um, we've reviewed it carefully [chuckles] . Honestly the team was impressed. The multi-tenant architecture is exactly what we need because we're running this on behalf of multiple end clients, not just internally."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Right, and that's where the white-label structure really matters. Each of your end clients gets their own isolated tenant. Their data never crosses into another client's environment. And from their perspective they're using Summit's branded platform, not ours... right?"},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "neutral",
             "text": "Our legal team flagged one thing — the data processing agreement. They want to make sure sub-processor relationships are clearly documented, particularly for the AI model providers you're using."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "We have a full sub-processor list and updated DPA that addresses exactly that. I'll have that to you by end of day today. Is your legal team working from a specific framework — GDPR, CCPA, or a custom internal standard?"},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "neutral",
             "text": "Combination. We have clients in the EU so GDPR compliance is non-negotiable. And our standard contract requires 30-day notice for any sub-processor changes."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "That's standard and we can accommodate it. The 30-day notice clause is something we've put in agreements before. I'll make sure the DPA version I send you reflects that. On the commercial side — did the preview engagement structure make sense to your team?"},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "positive",
             "text": "I mean, it did, and frankly it's what won you points basically over the competition. The fact that you're building against our — well, my team's actual data before we commit is a big deal. Two of the other vendors we looked at wanted a 12-month contract upfront."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "We built the preview like model specifically because we're confident in the results. We want you to see it working with your real calls before anyone talks about a long-term agreement. When would you want to kick off the discovery phase?"},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "positive",
             "text": "If the legal stuff resolves cleanly, we're targeting two weeks from now. I need the DPA and sub-processor list first, then I can get sign-off from our General Counsel and we're moving."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "You'll have everything you need today. As far as I know, i'm really excited about this one, Rachel. I think the results are gonna speak clearly."},
        ],
    },

    {
        "title": "Objection Handling — TechFlow CRM",
        "agent": "priya.sharma@callsight.ai",
        "customer_name": "Brian Walsh",
        "customer_company": "TechFlow CRM",
        "customer_title": "CTO",
        "duration_secs": 1980,
        "source": "zoom",
        "days_ago": 21,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Brian, thanks for reconnecting. I know from our last call you had some reservations — I wanted to give you a chance to put them directly on the table."},
            {"speaker_id": "customer", "speaker_name": "Brian Walsh",  "sentiment": "negative",
             "text": "Yeah, look — I'll be direct. The demo was impressive but my team looked at the API documentation and they think the integration lift is higher than you represented. We're a small engineering team and I can't have them tied up on a vendor integration for weeks."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "neutral",
             "text": "That's fair feedback and I wanna take it seriously. Can you tell me specifically which parts of the API spec they felt were heavier than expected? I wanna make sure I'm not minimizing something real."},
            {"speaker_id": "customer", "speaker_name": "Brian Walsh",  "sentiment": "negative",
             "text": "Look, the webhook configuration and the custom field mapping for our CRM schema. We have a fairly non-standard data model and they're worried about edge cases that your team hasn't hit before."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Okay, that's actually solvable. During the preview engagement we pair our integration specialist directly with your engineers. They're not just reading documentation — they're building it with you. And we've handled non-standard CRM schemas before; it's one of the things we built for."},
            {"speaker_id": "customer", "speaker_name": "Brian Walsh",  "sentiment": "neutral",
             "text": "I mean, what does that actually look like in practice? Because if it's 'we'll answer questions on a Slack channel' that's not what I need... if that makes sense?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "It's not that. It's scheduled working sessions — screen-shared, hands-on, your dev and ours in the same call writing code together. We own the outcome of the integration, not just the documentation. If something doesn't work, it's our problem to solve, not yours to figure out... right?"},
            {"speaker_id": "customer", "speaker_name": "Brian Walsh",  "sentiment": "neutral",
             "text": "Okay. That changes the picture somewhat. The other issue is we've had a previous vendor over-promise on accuracy and it embarrassed us in front of clients. How do I know your transcription accuracy claims are real?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Run it against your own calls during the preview. That's the whole point. We don't ask you to take our word for it — we run your actual audio through the system and you see the accuracy number on your specific data, with your accents, your domain vocabulary, your audio quality. If it doesn't hit the bar you need, we don't move forward."},
            {"speaker_id": "customer", "speaker_name": "Brian Walsh",  "sentiment": "neutral",
             "text": "That's a reasonable offer. Let me talk to my team. I'm not saying no, I'm saying we need to have a more technical conversation before I'm comfortable moving forward."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Completely fair. Let me set up a call directly between my integration lead and your engineers — no sales people, just technical folks working through the real questions. Can you intro me to whoever leads your platform team?"},
        ],
    },

    {
        "title": "White-Label Discovery — PinnacleConnect",
        "agent": "marcus.webb@callsight.ai",
        "customer_name": "Diane Foster",
        "customer_company": "PinnacleConnect",
        "customer_title": "Head of Product",
        "duration_secs": 2160,
        "source": "zoom",
        "days_ago": 11,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Diane, I've been looking forward to this. I know PinnacleConnect is one of the faster-growing CPaaS players in the mid-market space — tell me what's on your roadmap that brought you to this conversation... so yeah."},
            {"speaker_id": "customer", "speaker_name": "Diane Foster", "sentiment": "positive",
             "text": "Honestly, we're at a crossroads. Our core telephony product is solid, but we keep losing enterprise deals to Twilio and Vonage because they have AI features we can't match. I'm pretty sure specifically transcription, post-call analytics, agent scoring. We need to offer that, but building it from scratch is a 12 to 18 month project minimum."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "That timeline is exactly why the white-label model exists. You get a production-ready platform branded as PinnacleConnect. Your customers never know we're in the stack. And your roadmap accelerates by the length of time it would have taken you to build it."},
            {"speaker_id": "customer", "speaker_name": "Diane Foster", "sentiment": "positive",
             "text": "What does the branding customization actually look like? Not just logos — I mean deep customization. Colors, navigation structure, even the terminology we use in the UI."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "It's a full theming layer — colors, typography, logo, domain. Navigation labels are configurable, so if you use different terminology than us for concepts like 'calls' or 'insights,' those can be renamed. And the widget SDK lets you embed individual components directly into your existing product interface, so it feels native rather than an add-on window."},
            {"speaker_id": "customer", "speaker_name": "Diane Foster", "sentiment": "positive",
             "text": "The embedded widget piece is interesting. We have some enterprise customers who've specifically asked not to leave the main product interface for analytics [laughs] . That could be a real unlock for us."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "That's a very common ask at the enterprise tier. The widget SDK is designed exactly for that — you embed it in an iframe or via our JS component and it respects your session auth, your theme, and the user's permissions. It looks like it was always part of your product."},
            {"speaker_id": "customer", "speaker_name": "Diane Foster", "sentiment": "positive",
             "text": "Right, so, we'd also need to be able to set different feature access levels by client tier. Our SMB customers get basic transcription, enterprise gets the full AI suite."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Feature flags are built into the tenant settings model, so yes — you control what each of your customers sees. You can gate real-time transcription, sentiment analysis, coaching, all of it at the tenant level. That lets you build your own tiered pricing on top of us."},
            {"speaker_id": "customer", "speaker_name": "Diane Foster", "sentiment": "positive",
             "text": "Okay, so, this is sounding very aligned. What's the process for evaluating this properly before we'd commit to anything?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Well, we you know do a structured preview — no commitment required. We go deep on your specific setup, build against your data, and you see it working in your environment [laughs] . From there you have everything you need to make a real decision. I'd suggest we get your engineering lead and product team together for a technical session in the next week or two."},
        ],
    },

    {
        "title": "Competitive Evaluation Follow-Up — Meridian Insurance",
        "agent": "jordan.cole@callsight.ai",
        "customer_name": "Greg Santos",
        "customer_company": "Meridian Insurance",
        "customer_title": "VP of Operations",
        "duration_secs": 1440,
        "source": "phone",
        "days_ago": 18,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",   "sentiment": "positive",
             "text": "Greg, thanks for taking the call. When we spoke last month you mentioned you were running a parallel evaluation — I wanted to check in and see where things stood."},
            {"speaker_id": "customer", "speaker_name": "Greg Santos",   "sentiment": "neutral",
             "text": "Still evaluating, honestly. We've got three vendors on I mean the shortlist including you. The others are Observe.AI and a regional player. It's taking longer than I expected because our compliance team keeps adding questions."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",   "sentiment": "positive",
             "text": "That's completely understandable in insurance. Can I ask what questions compliance you know is focused on? I wanna make sure we've addressed them properly."},
            {"speaker_id": "customer", "speaker_name": "Greg Santos",   "sentiment": "neutral",
             "text": "Mostly around data residency. We have some state-level regulatory requirements that mean certain call data can't leave specific geographic boundaries. It's a hard constraint."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",   "sentiment": "positive",
             "text": "Data residency is something we can accommodate at the tenant configuration level. We have US-region-only deployments and we can discuss geo-fencing specific tenant data. That said, I want to make sure I give you an accurate answer rather than a salesy one — can I loop in our solutions architect to answer the technical specifics directly?"},
            {"speaker_id": "customer", "speaker_name": "Greg Santos",   "sentiment": "neutral",
             "text": "Yes, that would be helpful. The compliance team would want to speak to someone technical, not just take my word for it."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",   "sentiment": "positive",
             "text": "I'll set that up. One other question — when you look at the three vendors, is there anything where you feel the others have a clear advantage over us right now that I should know about?"},
            {"speaker_id": "customer", "speaker_name": "Greg Santos",   "sentiment": "neutral",
             "text": "Observe.AI has a longer track record in insurance specifically. They have case studies from carriers we recognize. You're newer and that gives some people on my team pause."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",   "sentiment": "positive",
             "text": "Right, so, that's a fair point and I won't pretend otherwise. What I'd say is that our preview model exists precisely for that reason. You don't have to take our word for it or read case studies — you see it working on your actual calls. That's a different kind of proof than a reference customer."},
            {"speaker_id": "customer", "speaker_name": "Greg Santos",   "sentiment": "neutral",
             "text": "Right, so, that's a decent counter-argument. Let's get the technical call scheduled and go from there."},
        ],
    },

    {
        "title": "Executive Briefing — Coastal Lending Group",
        "agent": "priya.sharma@callsight.ai",
        "customer_name": "Maria Vasquez",
        "customer_company": "Coastal Lending Group",
        "customer_title": "Director of Sales",
        "duration_secs": 2520,
        "source": "zoom",
        "days_ago": 2,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma",  "sentiment": "positive",
             "text": "Maria, welcome — really glad you were able to join with your CMO. I know time is tight so I'll keep this tight and focused on outcomes... so yeah."},
            {"speaker_id": "customer", "speaker_name": "Maria Vasquez", "sentiment": "positive",
             "text": "Okay, so, perfect. I've briefed David on what we've seen so far. He has some pointed questions but the direction is positive... right?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma",  "sentiment": "positive",
             "text": "Right, right. David, great to have you. I'd love to hear what you're most focused on from where you sit."},
            {"speaker_id": "customer", "speaker_name": "Maria Vasquez", "sentiment": "positive",
             "text": "I'll let Maria speak to the operational side, but from my perspective it's two things: visibility  — sorry, someone just walked in —  into what our — well, my team's loan officers are actually saying on calls, and a defensible way to reduce compliance risk from verbal commitments made in sales conversations."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma",  "sentiment": "positive",
             "text": "Both of those are direct use cases. On visibility — every call gets transcribed and analyzed. Managers have a dashboard showing talk patterns, sentiment trends, and specific calls that need attention, flagged automatically. You stop randomly sampling 3 percent of calls and start reviewing 100 percent of them intelligently. On compliance — the transcript creates a permanent, searchable record of verbal commitments. You can search across all calls for specific phrases or commitments made about rates, terms, timelines."},
            {"speaker_id": "customer", "speaker_name": "Maria Vasquez", "sentiment": "positive",
             "text": "The searchable record piece is actually something our legal team has been asking for separately. Right now if there's a dispute, we have no way to retrieve what was said on a specific call three months ago."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma",  "sentiment": "positive",
             "text": "Mhm. That's a very concrete problem we solve. The transcript archive is fully searchable — you can query by date range, rep, sentiment, keyword, or topic. And everything is retained based on your configured retention policy. If there's ever a dispute, you pull the transcript in seconds."},
            {"speaker_id": "customer", "speaker_name": "Maria Vasquez", "sentiment": "positive",
             "text": "David, what do you think? This seems to address the compliance question you raised in our pre-call."},
            {"speaker_id": "customer", "speaker_name": "Maria Vasquez", "sentiment": "positive",
             "text": "It does. If I'm not mistaken, priya, what would it take to get a pilot running? I'm not interested in another six-month evaluation cycle."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma",  "sentiment": "positive",
             "text": "Well, our preview engagement is built for exactly that impatience — which I mean as a compliment. We do a discovery session, build against your actual call data, and you're seeing real output within the first few weeks. No six-month cycle. I'd suggest we book the discovery session this week while momentum is high."},
            {"speaker_id": "customer", "speaker_name": "Maria Vasquez", "sentiment": "positive",
             "text": "Yeah, so, let's do it. Send me the calendar link and I'll get the right people from our side on it."},
        ],
    },

    {
        "title": "Large Deal Discovery — Global Support Hub",
        "agent": "marcus.webb@callsight.ai",
        "customer_name": "Andre Williams",
        "customer_company": "Global Support Hub",
        "customer_title": "VP of Customer Experience",
        "duration_secs": 3600,
        "source": "zoom",
        "days_ago": 9,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Um, andre, thank you for making time. I understand Global Support Hub operates contact centers for multiple Fortune 500 clients — can you give me a sense of the scale we're talking about?"},
            {"speaker_id": "customer", "speaker_name": "Andre Williams", "sentiment": "neutral",
             "text": "Sure. We run about 12 contact centers globally, roughly 3,500 agents total. We handle customer service for about 18 enterprise clients across retail, telco, and financial services. Each client has different requirements, different KPIs, different tooling preferences."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "That multi-client complexity is actually where we fit well. Each of your end clients can have their own isolated tenant with separate branding, separate data, separate reporting. From their perspective it's their branded analytics platform. From your perspective it's one system you manage across all of them."},
            {"speaker_id": "customer", "speaker_name": "Andre Williams", "sentiment": "positive",
             "text": "Alright, so, that's interesting. Right now we use three different tools across our client base because some clients mandate specific vendors. That fragmentation is killing my ops team."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Consolidating onto one platform with white-label fronts per client is exactly the use case. The underlying system is the same — your team learns one thing. The client sees their own branded version. And the reporting you give clients comes out of a single data model, so comparisons and benchmarks across your portfolio become possible."},
            {"speaker_id": "customer", "speaker_name": "Andre Williams", "sentiment": "positive",
             "text": "Alright, so, portfolio benchmarking — that's actually a selling point we could use with our own clients. 'Your CSAT trends compared to our median across the portfolio' — that's valuable data we can't produce today."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Okay, so, exactly. And because it's built on standardized AI analysis — same sentiment model, same topic extraction — comparisons are apples-to-apples. Right now your tools are producing metrics that can't be compared because they're measured differently."},
            {"speaker_id": "customer", "speaker_name": "Andre Williams", "sentiment": "positive",
             "text": "Alright, so, how does pricing work for something at our — well, my team's scale? I want to make sure we're not having a conversation that collapses when numbers come out."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Um, i want to be straightforward with you — pricing for a deployment at your scale is custom and we don't get to that conversation until after the preview, when we both have real data on actual usage. What I can tell you is that at your volume, the economics are typically very favorable relative to what you're spending across the multiple tools you're trying to consolidate. [chuckles] "},
            {"speaker_id": "customer", "speaker_name": "Andre Williams", "sentiment": "positive",
             "text": "Fair. I respect not leading with a number before either of us knows the real scope. What would the preview look like for us?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Okay, okay [chuckles] . We'd start with one or two of your clients — probably the ones with the clearest pain around call quality — and run the full discovery and build process on those. Once you see results there, you have a real basis for deciding how broadly to roll it out across the portfolio. We move deliberately, not all at once."},
            {"speaker_id": "customer", "speaker_name": "Andre Williams", "sentiment": "positive",
             "text": "That's a sensible approach. I have two clients in mind immediately. Let me set up an internal alignment call with my ops directors and then let's get back on the calendar."},
        ],
    },

    {
        "title": "Cold Outreach — Vantage Real Estate Group",
        "agent": "jordan.cole@callsight.ai",
        "customer_name": "Linda Park",
        "customer_company": "Vantage Real Estate Group",
        "customer_title": "Sales Director",
        "duration_secs": 660,
        "source": "phone",
        "days_ago": 30,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",  "sentiment": "positive",
             "text": "Hi Linda, this is Jordan Cole from CallSight AI. I'll keep it short — we work with sales-driven organizations to automatically capture and analyze what's happening on their calls. Given the volume of buyer conversations your agents are having, I thought it might be worth a quick conversation."},
            {"speaker_id": "customer", "speaker_name": "Linda Park",   "sentiment": "neutral",
             "text": "So, i'm familiar with these kinds of tools but we haven't really looked at them seriously. What specifically do you do that's different?"},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",  "sentiment": "positive",
             "text": "The main differentiator is that we don't just transcribe and surface a dashboard — we extract specific action items from each call and assign them automatically, so your agents aren't losing follow-ups between calls. In real estate that can be a significant revenue leak... if that makes sense?"},
            {"speaker_id": "customer", "speaker_name": "Linda Park",   "sentiment": "neutral",
             "text": "So, follow-up is absolutely a problem. We lose deals because agents don't log what they promised to do. That's a real thing. What's the typical implementation lift?"},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",  "sentiment": "positive",
             "text": "It's lighter than most people expect. If you're using a standard CRM — Salesforce, HubSpot — the integration is days not weeks. And we do a structured preview before any financial commitment, so you see it working with your actual data first."},
            {"speaker_id": "customer", "speaker_name": "Linda Park",   "sentiment": "neutral",
             "text": "Okay, so, i'm not in the right headspace to evaluate something new right now — Q2 is chaotic. But I'm not saying no. Can you reach back out in about six weeks?"},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",  "sentiment": "positive",
             "text": "Absolutely. I'll put a note to follow up in late May. And I'll send a brief one-pager in the meantime so it's on your radar. Thanks for the honest response, Linda — I appreciate it."},
        ],
    },

    {
        "title": "Technical Deep Dive — Apex Communications (Follow-Up)",
        "agent": "marcus.webb@callsight.ai",
        "customer_name": "Kevin Okafor",
        "customer_company": "Apex Communications",
        "customer_title": "VP of Product",
        "duration_secs": 4200,
        "source": "zoom",
        "days_ago": 1,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Kevin, thanks for getting the engineering team on. I'll hand it over to Sam from our integration side in a minute — but wanted to set context. The goal today is for your platform engineering team to evaluate the integration architecture directly, no sales filter... right?"},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor",  "sentiment": "positive",
             "text": "Appreciated. I've Raj and Tanya from our platform team here. They have real questions so I'll let them drive."},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor",  "sentiment": "neutral",
             "text": "First question from Raj — your webhook model for call events. What's the retry behavior if our endpoint is down, and how do you handle ordering guarantees?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Great question. Sam, do you want to take that?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "So, so on retry — we use exponential backoff with up to seven attempts over 24 hours. Events are delivered with a sequence ID and timestamp, and there's a webhook reconciliation endpoint your system can call to re-fetch any events in a time window if you detect a gap. Ordering is best-effort but not guaranteed at delivery — if strict ordering matters for your use case we recommend processing via the sequence ID on your end."},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor",  "sentiment": "positive",
             "text": "That's reasonable. Tanya's question — authentication model for the API. We'd be calling from our backend on behalf of many tenants. What's the auth pattern?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Sure, sure. Each tenant gets their own API key scoped to their tenant context. From your platform's perspective you'd manage a mapping between your internal client IDs and the corresponding API keys. Alternatively, if you're operating as a platform partner rather than end tenant, we can issue you a platform-level credential with tenant scoping on each request header. That's probably cleaner for your architecture."},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor",  "sentiment": "positive",
             "text": "Look, platform-level credential sounds right. Last one — latency on real-time transcription [chuckles] . We have clients who want in-call alerts for compliance keywords. What's the end-to-end lag?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Streaming transcription has roughly 300 to 500 millisecond latency on the transcript delivery. Keyword alert triggering runs off the streaming buffer with comparable lag. So for a compliance keyword hit, your system gets the webhook in under a second of the word being spoken in most conditions."},
            {"speaker_id": "customer", "speaker_name": "Kevin Okafor",  "sentiment": "positive",
             "text": "That's workable. Raj, Tanya — anything blocking from your perspective? Okay. Marcus, I think we're good to move to the next stage. What does that look like? [laughs] "},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",   "sentiment": "positive",
             "text": "Yeah. We kick off the discovery session — structured interviews with your product and ops teams, then we start building your branded environment. Given today's technical conversation I feel very confident about the integration path."},
        ],
    },

    {
        "title": "Contract Finalization — Summit BPO Partners",
        "agent": "marcus.webb@callsight.ai",
        "customer_name": "Rachel Chen",
        "customer_company": "Summit BPO Partners",
        "customer_title": "COO",
        "duration_secs": 2100,
        "source": "zoom",
        "days_ago": 1,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Rachel, I'm glad we got the DPA resolved quickly. Your legal team was thorough but efficient — that was a good process."},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "positive",
             "text": "Alright, so, they move fast when the documentation is clean. I'll be honest — they were expecting more back-and-forth. The fact that your sub-processor list was complete and the 30-day change notice was already drafted made their job easy."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Ah, I see. We've been through enough legal reviews to know what a well-prepared company looks like. Are we clear to sign the preview agreement today, or is there anything still open?"},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "positive",
             "text": "Okay, so, we're clear. General Counsel signed off this morning. I have the agreement open right now. One last question — the SOW for the discovery phase. It references stakeholder interviews and a systems audit. Who from your side is running that?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Mhm. I'll be running the discovery sessions personally, alongside our solutions architect. The systems audit's technical so he'll take the lead on that. For your side we'd typically want to interview the operations director, two or three of your frontline supervisors, and your IT lead who owns the telephony stack."},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "positive",
             "text": "So, that's exactly the right people. I can have them available. When do you want to start?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",  "sentiment": "positive",
             "text": "Got it. We can kick off next week. I'll send calendar invites for the first two interview sessions by end of today. This is a big deal for us, Rachel — we're gonna make you look great to your clients."},
            {"speaker_id": "customer", "speaker_name": "Rachel Chen",  "sentiment": "positive",
             "text": "That's the goal. Looking forward to it."},
        ],
    },

    {
        "title": "Re-Engagement Call — TechFlow CRM",
        "agent": "priya.sharma@callsight.ai",
        "customer_name": "Brian Walsh",
        "customer_company": "TechFlow CRM",
        "customer_title": "CTO",
        "duration_secs": 960,
        "source": "phone",
        "days_ago": 6,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Brian, thanks for taking the call. I know we went quiet after our last conversation — I wanted to reconnect and see if anything had changed on your end."},
            {"speaker_id": "customer", "speaker_name": "Brian Walsh",  "sentiment": "neutral",
             "text": "Honestly, yes. We had a rough quarter. Customer churn ticked up and my CEO is now asking hard questions about uh whether our onboarding calls are setting the right expectations. It's a problem I need to solve."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Yeah, so, that's a very specific use case and one we can address directly. If the problem is that onboarding calls aren't consistent or aren't setting clear expectations, we can analyze every onboarding call, flag where expectation gaps are occurring, and give you a pattern across your whole team. From what I remember, you stop guessing at what's causing churn and you know start seeing it in the call data."},
            {"speaker_id": "customer", "speaker_name": "Brian Walsh",  "sentiment": "positive",
             "text": "That's actually a more compelling pitch than what you gave me last time. Last time it felt like a general analytics play. This feels like it solves the problem I've right now."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Well, i should have led with the business problem earlier — that's on me. The onboarding call analysis like is very concrete. We can run a sample set of your past onboarding calls through the system and show you specific patterns in the first week of the preview. Is that worth a fresh conversation with your team?"},
            {"speaker_id": "customer", "speaker_name": "Brian Walsh",  "sentiment": "positive",
             "text": "Right, so, yeah, it is. Let's get that technical session back on the calendar. The question from my engineering team hasn't gone away but the business case just got a lot clearer to me."},
        ],
    },

    {
        "title": "Partner Program Pitch — RegionTel",
        "agent": "marcus.webb@callsight.ai",
        "customer_name": "Frank Dominguez",
        "customer_company": "RegionTel",
        "customer_title": "VP of Partnerships",
        "duration_secs": 2400,
        "source": "zoom",
        "days_ago": 15,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",    "sentiment": "positive",
             "text": "Frank, appreciate you joining. I'll be direct — I reached out because RegionTel sits in so an interesting position in the market. You're a regional carrier with real enterprise relationships and your customers are asking for intelligence features you don't natively offer. That's the exact wedge we're built for."},
            {"speaker_id": "customer", "speaker_name": "Frank Dominguez","sentiment": "positive",
             "text": "You did your homework. Yes, we're fielding that request constantly, particularly from our healthcare and financial services clients. We've looked at a couple of solutions but they all wanted us to resell their branded product. We're not a reseller — we're a platform."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",    "sentiment": "positive",
             "text": "Gotcha. Exactly the distinction that matters. We're not asking you to resell CallSight. We're asking you to offer your own call intelligence product, powered by our infrastructure. Your brand, your packaging, your pricing to your clients. We're invisible in the stack."},
            {"speaker_id": "customer", "speaker_name": "Frank Dominguez","sentiment": "positive",
             "text": "That changes the conversation significantly. What does the economics model look like for a partner?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",    "sentiment": "positive",
             "text": "Okay, so, we work on a wholesale model — you're buying the capability at a volume rate and packaging it however you want for your clients. The margin is yours to structure. But I wanna be transparent: we don't discuss the commercial terms until after the preview, when we both understand the real usage profile. Too many deals fall apart because numbers are thrown out before the scope is clear."},
            {"speaker_id": "customer", "speaker_name": "Frank Dominguez","sentiment": "positive",
             "text": "I respect that approach. I've seen vendors throw out a per-minute number that sounds great and then reality is very different once you're in it. What does the preview look like for a partner like us?"},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",    "sentiment": "positive",
             "text": "We'd pick one of your existing enterprise clients as the pilot — ideally one that's already asked for this capability. As far as I know, we build a branded environment for them, run their calls through it, and you get to see both the product working and the client reaction. It's the fastest path to a real commercial conversation."},
            {"speaker_id": "customer", "speaker_name": "Frank Dominguez","sentiment": "positive",
             "text": "I've a client in mind immediately. Healthcare network, about 300 agents, has been asking us for exactly this. Let me see if I can get them to agree to be the pilot. If they're in, we're in."},
            {"speaker_id": "agent",    "speaker_name": "Marcus Webb",    "sentiment": "positive",
             "text": "That's a great anchor client for a pilot. Let me send you a brief overview deck you can use when you have that conversation with them. And let's book a follow-up for next week."},
        ],
    },

    {
        "title": "Compliance-Focused Discovery — CareNetwork Health",
        "agent": "jordan.cole@callsight.ai",
        "customer_name": "Stephanie Moss",
        "customer_company": "CareNetwork Health",
        "customer_title": "Chief Compliance Officer",
        "duration_secs": 2700,
        "source": "zoom",
        "days_ago": 23,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "Stephanie, thank you for agreeing to this call. I know as CCO your time is precious and your threshold for vendor conversations is high. I'll try to make this worth your time."},
            {"speaker_id": "customer", "speaker_name": "Stephanie Moss", "sentiment": "neutral",
             "text": "I'll be straight with you — I'm here because our VP of Operations has been pushing for a call analytics solution and I need to make sure we don't create a compliance exposure in the process. I'm pretty sure i'm not you know evaluating the product, I'm evaluating the risk."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "That's exactly the right framing and I wanna address it on your terms. What are your top two or three specific concerns?"},
            {"speaker_id": "customer", "speaker_name": "Stephanie Moss", "sentiment": "neutral",
             "text": "Alright, so, pHI in call transcripts, specifically whether AI models are being trained on our patient data. That's a hard no for us. Second is data residency — we have state agreements that restrict where certain data can be stored. Third is audit trail — if there's ever a regulatory inquiry I need to be able to demonstrate that data was handled properly at every step."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "Got it. Let me take those one at a time. On model training — your data is never used to train any AI model, ours or our providers'. Full stop. That's in the data processing agreement and we can make it explicit in a custom addendum if needed. On residency — we support US-only data processing and can configure geographic restrictions at the tenant level. I'll need to know the specific state agreements you're referencing to confirm coverage. On audit trail — every data access, processing event, and API call is logged and exportable. We can produce a full audit trail for any time window you specify... you know what I mean?"},
            {"speaker_id": "customer", "speaker_name": "Stephanie Moss", "sentiment": "neutral",
             "text": "The training data answer is the right answer. The audit trail capability is stronger than I expected. The state residency question — we operate under agreements in California, New York, and Texas. I'll need documented confirmation on those specifically."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",    "sentiment": "positive",
             "text": "I can get you a written statement on those three states by end of week. I'd also suggest we schedule a technical session with our security architect who can answer your questions directly rather than me paraphrasing him."},
            {"speaker_id": "customer", "speaker_name": "Stephanie Moss", "sentiment": "neutral",
             "text": "That would be more useful, yes. I want to hear answers from someone who actually built the system. If that conversation satisfies my questions, I'll give our operations team a green light to proceed."},
        ],
    },

    {
        "title": "Enthusiastic Demo — CloudServe SaaS",
        "agent": "priya.sharma@callsight.ai",
        "customer_name": "Jason Kim",
        "customer_company": "CloudServe SaaS",
        "customer_title": "VP of Revenue",
        "duration_secs": 3120,
        "source": "zoom",
        "days_ago": 4,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Um, jason, excited for this one. I've been looking at CloudServe's growth trajectory — you've scaled the sales team pretty aggressively in the last 18 months. How has keeping quality consistent across a growing team been?"},
            {"speaker_id": "customer", "speaker_name": "Jason Kim",    "sentiment": "positive",
             "text": "Okay, so, honestly it's been chaos in the best way. We went from 8 reps to 34 and coaching just hasn't scaled. My VP of Sales is doing one-on-ones with 34 people trying to manually review calls and she's burning out. We need to automate the intelligence layer."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Mhm. That's a textbook scaling problem and exactly what we're designed for [laughs] . Let me show you what your VP of Sales' life looks like with CallSight."},
            {"speaker_id": "customer", "speaker_name": "Jason Kim",    "sentiment": "positive",
             "text": "Please. She told me to come to this demo and 'fix the problem' so I'm motivated."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Okay, okay. So in the team performance view, every rep has a score that updates after every call. It's not a vanity metric — it's based on specific behaviors: talk-to-listen ratio, question frequency, how often they confirm next steps, objection handling patterns. If I'm not mistaken, she can sort by score, filter to this week, and see exactly which reps need attention and for what reason. Instead of reviewing 34 one-on-ones, she's prioritizing her time to the calls that actually need her."},
            {"speaker_id": "customer", "speaker_name": "Jason Kim",    "sentiment": "positive",
             "text": "This is going to make her cry tears of joy. Can she listen to the specific moments in a call that triggered a coaching flag?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Yes — every coaching note links to the transcript segment that generated it. She can jump directly to that moment in the recording. So instead of listening to an entire 45-minute call, she jumps to the two minutes that matter."},
            {"speaker_id": "customer", "speaker_name": "Jason Kim",    "sentiment": "positive",
             "text": "What about deal intelligence? Our sales managers also need to know which deals are at risk. Can this surface that?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Call-level sentiment trends are a strong leading indicator. If a deal has gone from positive sentiment in early calls to neutral or negative in recent ones, that pattern gets flagged. Combined with the action item tracking — whether committed follow-ups are actually getting done — it's a reasonable deal risk signal without requiring CRM hygiene to be perfect."},
            {"speaker_id": "customer", "speaker_name": "Jason Kim",    "sentiment": "positive",
             "text": "We use HubSpot. Does this push into HubSpot natively?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Full native integration. Call summary, sentiment score, action items, and coaching notes all push into the HubSpot activity record automatically. Your reps barely need to touch it."},
            {"speaker_id": "customer", "speaker_name": "Jason Kim",    "sentiment": "positive",
             "text": "I want I mean to move on this fast. What's the path to getting started?"},
        ],
    },

    {
        "title": "Low-Interest Cold Call — Nationwide Adjusters",
        "agent": "jordan.cole@callsight.ai",
        "customer_name": "Phil Turner",
        "customer_company": "Nationwide Adjusters",
        "customer_title": "Operations Manager",
        "duration_secs": 420,
        "source": "phone",
        "days_ago": 38,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",  "sentiment": "positive",
             "text": "Hi Phil, Jordan Cole from CallSight AI. We build call analytics software for operations teams handling high call volume. Is this a bad time?"},
            {"speaker_id": "customer", "speaker_name": "Phil Turner",  "sentiment": "negative",
             "text": "Depends on what you're selling. We get I mean a lot of these calls."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",  "sentiment": "neutral",
             "text": "Fair enough. The short version is — we automatically transcribe and analyze calls, surface quality issues, and generate action items without anyone having to manually review anything. For adjusters handling high volumes of claimant calls it typically flags compliance and coaching issues that manual review misses."},
            {"speaker_id": "customer", "speaker_name": "Phil Turner",  "sentiment": "negative",
             "text": "We already have a call recording system and my supervisors do random audits. I don't see the problem you're solving for us."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",  "sentiment": "neutral",
             "text": "Random audits cover maybe three — no wait, to five percent of calls. If a compliance issue happens in the 95 percent you're not reviewing, you won't know until a complaint comes in. Is that a risk you're comfortable with?"},
            {"speaker_id": "customer", "speaker_name": "Phil Turner",  "sentiment": "negative",
             "text": "That's a scare tactic. We've been doing it this way basically for 20 years and it works fine. I'm not interested."},
            {"speaker_id": "agent",    "speaker_name": "Jordan Cole",  "sentiment": "neutral",
             "text": "Mhm. Well, understood, and I appreciate your directness. I'll leave the door open in case anything changes. Have a good day, Phil."},
        ],
    },

    {
        "title": "Staffing Industry Discovery — TalentBridge Staffing",
        "agent": "priya.sharma@callsight.ai",
        "customer_name": "Nadia Osei",
        "customer_company": "TalentBridge Staffing",
        "customer_title": "VP of Business Development",
        "duration_secs": 1800,
        "source": "zoom",
        "days_ago": 26,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Um, nadia, thanks for making time. I'll be honest — staffing isn't our most common vertical so I'm genuinely curious about your use case before I say anything about uh our product."},
            {"speaker_id": "customer", "speaker_name": "Nadia Osei",   "sentiment": "positive",
             "text": "Alright, so, i appreciate that. Here's the thing — our — well, my team's business development team does a huge volume of intake calls with hiring managers and candidate interviews. The information captured on those calls is critical but we've no systematic way to extract it or track follow-through... does that make sense?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "That's a really uh interesting use case. You're essentially using calls as intake forms, but the structured data is getting lost in the conversation. Can you give me a concrete example?"},
            {"speaker_id": "customer", "speaker_name": "Nadia Osei",   "sentiment": "positive",
             "text": "Sure. A BD rep talks to a hiring manager for 30 minutes. I believe the manager describes five specific things they need in a candidate. That information goes into notes — if the rep bothers — and then disappears. Three weeks later a different recruiter is working the role with no context. We're constantly reinventing the wheel."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "So, the action item extraction and structured summary features map directly onto that problem. Every call produces a summary of what was discussed and a list of extracted commitments or requirements. If the manager said they need someone with five years of SAP experience and availability to start in three weeks, that's in the action item list automatically. Searchable and assigned."},
            {"speaker_id": "customer", "speaker_name": "Nadia Osei",   "sentiment": "positive",
             "text": "And does this integrate with ATS systems? We use Bullhorn."},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "neutral",
             "text": "Bullhorn isn't on our current native integration list but we've a webhook and REST API layer that can push structured data to any system with an API. Bullhorn has an API so it would be a custom integration — not a flip-a-switch situation but also not a six-month project."},
            {"speaker_id": "customer", "speaker_name": "Nadia Osei",   "sentiment": "positive",
             "text": "That's honest. Our CTO would need to weigh in on the integration side. But the concept is compelling enough to take to him. Can you send me a brief summary I can share with him?"},
            {"speaker_id": "agent",    "speaker_name": "Priya Sharma", "sentiment": "positive",
             "text": "Well, absolutely. I'll put together a one-pager specific to your staffing workflow — intake call extraction, recruiter handoff use case, structured requirement capture. And I'd love to get a three-way call on the calendar with you and your CTO in the next couple of weeks."},
        ],
    },

]

# ── Database functions ────────────────────────────────────────────────────────

def run_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()
    print("✓ Schema created")

def insert_tenant(conn):
    tid = new_id()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name, slug, plan) VALUES (%s, %s, %s, %s)",
            (tid, "CallSight Demo", "callsight-demo", "enterprise")
        )
    conn.commit()
    print(f"✓ Tenant inserted: {tid}")
    return tid

def insert_users(conn, tenant_id):
    user_map = {}
    with conn.cursor() as cur:
        for a in AGENTS:
            uid = new_id()
            cur.execute(
                "INSERT INTO users (id, tenant_id, email, full_name, role, department) VALUES (%s,%s,%s,%s,%s,%s)",
                (uid, tenant_id, a["email"], a["full_name"], a["role"], a["department"])
            )
            user_map[a["email"]] = uid
    conn.commit()
    print(f"✓ {len(AGENTS)} users inserted")
    return user_map

def insert_calls(conn, tenant_id, user_map):
    total_calls = 0
    total_segs  = 0

    with conn.cursor() as cur:
        for c in SALES_CALLS:
            call_id      = new_id()
            transcript_id = new_id()
            agent_uid    = user_map[c["agent"]]
            call_date    = days_ago(c["days_ago"])
            participants = [
                {"name": c["customer_name"], "company": c["customer_company"],
                 "title": c["customer_title"], "role": "customer"},
                {"name": next(a["full_name"] for a in AGENTS if a["email"] == c["agent"]),
                 "role": "agent"},
            ]

            # Call row
            cur.execute("""
                INSERT INTO calls
                  (id, tenant_id, uploaded_by, title, call_type, participants,
                   duration_secs, source, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                call_id, tenant_id, agent_uid,
                c["title"], "sales",
                json.dumps(participants),
                c["duration_secs"], c["source"], "complete",
                call_date, call_date,
            ))

            # Build full transcript text and segments
            segments = c["segments"]
            full_text = build_full_text(segments)
            word_count = len(full_text.split())

            cur.execute("""
                INSERT INTO transcripts
                  (id, call_id, tenant_id, full_text, word_count, engine)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (transcript_id, call_id, tenant_id, full_text, word_count, "whisper"))

            cursor_ms = 0
            for i, seg in enumerate(segments):
                seg_id  = new_id()
                end_ms  = ms_from_words(seg["text"], cursor_ms)
                cur.execute("""
                    INSERT INTO transcript_segments
                      (id, transcript_id, speaker_id, speaker_name,
                       start_ms, end_ms, text, sentiment, seq_order)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    seg_id, transcript_id,
                    seg["speaker_id"], seg["speaker_name"],
                    cursor_ms, end_ms,
                    seg["text"], seg["sentiment"], i,
                ))
                cursor_ms = end_ms + random.randint(300, 1200)
                total_segs += 1

            total_calls += 1

    conn.commit()
    print(f"✓ {total_calls} sales calls inserted ({total_segs} transcript segments)")

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\nConnecting to Neon (Flex)...")
    conn = psycopg2.connect(DB_URL)
    print("✓ Connected\n")

    run_schema(conn)
    tenant_id = insert_tenant(conn)
    user_map  = insert_users(conn, tenant_id)
    insert_calls(conn, tenant_id, user_map)

    # Summary
    with conn.cursor() as cur:
        for table in ("tenants", "users", "calls", "transcripts", "transcript_segments"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"  {table}: {cur.fetchone()[0]} rows")

    conn.close()
    print("\n✓ Done. Ready for AI analysis pass.\n")

if __name__ == "__main__":
    main()
