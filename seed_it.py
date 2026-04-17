#!/usr/bin/env python3
"""
seed_it.py — Seed 15 IT support calls into the CallSight demo database.
Schema already exists (created by seed_sales.py).
Tenant and users fetched from DB.
"""

import os, uuid, json, psycopg2
from datetime import datetime, timedelta

# ── Load .env manually ───────────────────────────────────────────────────────
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_5jYJVhez6aGO@ep-icy-hill-adp1qyi8-pooler.c-2.us-east-1.aws.neon.tech/neondb?sslmode=require",
)

# ── Helpers ───────────────────────────────────────────────────────────────────
def new_id():
    return str(uuid.uuid4())

def ms_from_words(text, start_ms):
    words = len(text.split())
    duration_ms = int((words / 140) * 60 * 1000)
    return start_ms + max(duration_ms, 1500)

def build_full_text(segments):
    return " ".join(f"{s['speaker_name']}: {s['text']}" for s in segments)

def days_ago(n):
    return datetime.utcnow() - timedelta(days=n)

# ── Fetch existing tenant + users ─────────────────────────────────────────────
def fetch_tenant_and_users(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tenants WHERE slug = 'callsight-demo'")
        tenant_id = cur.fetchone()[0]
        cur.execute("SELECT email, id FROM users WHERE tenant_id = %s", (tenant_id,))
        user_map = {row[0]: row[1] for row in cur.fetchall()}
    return tenant_id, user_map

# ── IT Support call data ──────────────────────────────────────────────────────
IT_CALLS = [
    {
        "title": "Twilio Webhook Misconfiguration — Calls Not Routing",
        "agent_email": "sam.patel@callsight.ai",
        "customer_name": "Raj Anand",
        "customer_company": "Horizon Telecom",
        "customer_title": "VoIP Engineer",
        "duration_secs": 1380,
        "source": "phone",
        "days_ago": 82,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Thanks for calling CallSight technical support, this is Sam. Can I get your name and company?"},
            {"speaker_id": "customer", "speaker_name": "Raj Anand", "sentiment": "negative",
             "text": "I mean, raj Anand at Horizon Telecom. I'm pretty sure we've been trying to get our Twilio integration working for two days and so calls are just not getting ingested. Nothing is showing up in the dashboard."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Okay, let's dig into this. First — are you seeing any webhook delivery attempts on the Twilio side, or is it failing before that?"},
            {"speaker_id": "customer", "speaker_name": "Raj Anand", "sentiment": "negative",
             "text": "Twilio shows the webhooks are being delivered. HTTP 200 responses. But nothing is in CallSight. I double-checked the endpoint URL in our Twilio console."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Makes sense. Interesting — the 200 means our server received it. Can you read me the webhook URL you're using in Twilio?"},
            {"speaker_id": "customer", "speaker_name": "Raj Anand", "sentiment": "neutral",
             "text": "Sure — it's https://api.callsight.ai/webhooks/twilio/calls. Is that right?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Right, so, almost. The correct endpoint is /webhooks/twilio/ingest — not /calls. The /calls endpoint is a legacy path that returns 200 but doesn't process anything. Classic documentation mismatch I've seen before."},
            {"speaker_id": "customer", "speaker_name": "Raj Anand", "sentiment": "positive",
             "text": "Oh wow. Two days on that. Let me update it right now."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "While you do that — also confirm your Twilio account SID is set in your CallSight integration settings. Go to Admin > Integrations > Twilio and make sure the SID matches exactly."},
            {"speaker_id": "customer", "speaker_name": "Raj Anand", "sentiment": "neutral",
             "text": "Checking... yes, SID is there. Okay, webhook URL is updated. I'll make a test call now... you know what I mean?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "Right, so, perfect. Give it 60 to 90 seconds to process after the call ends. I'll stay on the line."},
            {"speaker_id": "customer", "speaker_name": "Raj Anand", "sentiment": "positive",
             "text": "It appeared. I can see the call in the dashboard right now. Thank you — I can't believe it was just the endpoint path."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "Easy fix once you know where to look. I'll flag this to our docs team — the legacy endpoint needs a deprecation notice. I think anything else I can help with today?"},
            {"speaker_id": "customer", "speaker_name": "Raj Anand", "sentiment": "positive",
             "text": "No, that's all I needed. Really appreciate the help."},
        ],
    },
    {
        "title": "SSO Setup — Okta SAML Configuration",
        "agent_email": "casey.rivera@callsight.ai",
        "customer_name": "Lauren Moss",
        "customer_company": "BrightPath Financial",
        "customer_title": "IT Security Lead",
        "duration_secs": 2160,
        "source": "video",
        "days_ago": 77,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Casey Rivera, CallSight integrations. Hey Lauren, thanks for scheduling this. From what I remember, you're working on the Okta SSO setup?"},
            {"speaker_id": "customer", "speaker_name": "Lauren Moss", "sentiment": "neutral",
             "text": "Yes. We're using Okta as our IdP and I've gone through the SAML configuration guide but authentication is failing at the assertion stage... right?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Okay. Can you share your screen so I can see the Okta app configuration and the error message?"},
            {"speaker_id": "customer", "speaker_name": "Lauren Moss", "sentiment": "neutral",
             "text": "Sure, sharing now. This is the SAML app I created in Okta. The ACS URL and entity ID are set as per your docs."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "I see it. Your ACS URL is correct. But look at your Name ID format — you have it set to EmailAddress but CallSight expects the persistent format [laughs] . That's why the assertion is failing."},
            {"speaker_id": "customer", "speaker_name": "Lauren Moss", "sentiment": "neutral",
             "text": "Okay, so, the docs say to use email. That's what I followed."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Um, yeah, there's an inconsistency in v2 of our SSO guide. The attribute mapping uses email but the Name ID format should be persistent. I'll make sure this gets corrected. Change that dropdown and let's test."},
            {"speaker_id": "customer", "speaker_name": "Lauren Moss", "sentiment": "neutral",
             "text": "Changed. I'm pretty sure testing login now... it redirected but I'm getting a 403 — insufficient permissions."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Um, that's a different issue — your Okta group isn't mapped to a CallSight role. In your attribute statements, add a group attribute called 'callsight_role' and assign the value 'admin' for your IT security group."},
            {"speaker_id": "customer", "speaker_name": "Lauren Moss", "sentiment": "neutral",
             "text": "Adding that now. Okay, saved. Retrying..."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Also — do you want to enforce SSO only or allow email/password as a fallback during rollout?"},
            {"speaker_id": "customer", "speaker_name": "Lauren Moss", "sentiment": "neutral",
             "text": "Fallback during rollout, then SSO-only after. Is that configurable?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Okay, so, yes, that's in Admin > Security > Authentication Policy. You can flip it once your full team is onboarded."},
            {"speaker_id": "customer", "speaker_name": "Lauren Moss", "sentiment": "positive",
             "text": "It worked! I'm in. Okay great, I'll roll this out to the rest of the team. The docs issue you mentioned — when will that be fixed?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "I'll submit the correction today and it usually updates within a few days. I'll email you once it's live. You're all set, Lauren."},
        ],
    },
    {
        "title": "API Rate Limiting — Hitting 429s on Bulk Transcript Pull",
        "agent_email": "sam.patel@callsight.ai",
        "customer_name": "Dmitri Volkov",
        "customer_company": "ProServe BPO",
        "customer_title": "Data Engineering Lead",
        "duration_secs": 1260,
        "source": "phone",
        "days_ago": 71,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "CallSight technical support, Sam speaking. What can I help you with today?"},
            {"speaker_id": "customer", "speaker_name": "Dmitri Volkov", "sentiment": "negative",
             "text": "Well, hi Sam, Dmitri from ProServe. We're getting 429 errors when we try to pull transcripts from the API. We're doing nightly batch exports to our data warehouse and the process is failing."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Got it. What endpoint are you hitting and roughly how many requests per minute?"},
            {"speaker_id": "customer", "speaker_name": "Dmitri Volkov", "sentiment": "neutral",
             "text": "It's GET /api/v1/transcripts with pagination. We're doing about 200 requests per minute to get through the full dataset."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "The default rate limit for transcript endpoints is 60 requests per minute on the enterprise tier. 200 RPM is going to hit that wall. There are two solutions — one is request throttling on your side, the other is switching to our bulk export endpoint."},
            {"speaker_id": "customer", "speaker_name": "Dmitri Volkov", "sentiment": "neutral",
             "text": "Um, what's the bulk export endpoint?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Makes sense. Right, so, it's POST /api/v1/exports — you send a date range and filter criteria, and we generate a compressed JSON file that you can download with a single GET. No rate limit issues. It's designed exactly for your use case."},
            {"speaker_id": "customer", "speaker_name": "Dmitri Volkov", "sentiment": "neutral",
             "text": "That would work. Is it documented?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Yes, it's in the API reference under Data Export. I'll send you a direct link. The async nature means you'll post the job, poll for status, then download when it's ready. Turnaround is typically under two minutes for a full month of data."},
            {"speaker_id": "customer", "speaker_name": "Dmitri Volkov", "sentiment": "positive",
             "text": "That's actually much cleaner than what we were doing. Can you raise our rate limit temporarily while we migrate?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "I can bump you to 120 RPM for the next 30 days on my end. I'll send a confirmation email. Should give you time to switch over without any more outages... if that makes sense?"},
            {"speaker_id": "customer", "speaker_name": "Dmitri Volkov", "sentiment": "positive",
             "text": "Perfect. Thanks, Sam. That solves both the immediate problem and the long-term one."},
        ],
    },
    {
        "title": "Audio Upload Failures — MP3 Files Rejected at Ingestion",
        "agent_email": "casey.rivera@callsight.ai",
        "customer_name": "Ingrid Svensson",
        "customer_company": "CoreTech Systems",
        "customer_title": "Operations Manager",
        "duration_secs": 1560,
        "source": "phone",
        "days_ago": 66,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Technical support, Casey Rivera. Go ahead."},
            {"speaker_id": "customer", "speaker_name": "Ingrid Svensson", "sentiment": "negative",
             "text": "Hi Casey. I believe we're trying to upload call recordings from our legacy system — they're MP3 files — and every single one is getting rejected. The error says 'unsupported format' but MP3 is a pretty standard format."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "You're right that we support MP3 [chuckles] . Can you tell me how these recordings were encoded? Specifically the bitrate and whether they're mono or stereo?"},
            {"speaker_id": "customer", "speaker_name": "Ingrid Svensson", "sentiment": "neutral",
             "text": "Right, so, i don't know off the top of my head. I'm pretty sure how would I check?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "If you're on a Mac, right-click the file and Get Info — it'll show the audio format details. I'm pretty sure on Windows, right-click > Properties > Details tab."},
            {"speaker_id": "customer", "speaker_name": "Ingrid Svensson", "sentiment": "neutral",
             "text": "Okay, checking... it says 8 kHz, mono, 8-bit."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "That's the issue. Our transcription engine requires a minimum of 16 kHz sample rate. From what I remember, 8 kHz is telephone-quality audio from the 90s — our speech recognition model can't reliably process it at that fidelity."},
            {"speaker_id": "customer", "speaker_name": "Ingrid Svensson", "sentiment": "negative",
             "text": "Look, these are all of our historical call recordings. We can't just lose them."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Right, so, i understand. The good news is you can resample them using ffmpeg — it's a free command-line tool. I can send you a batch conversion script that will upsample your files to 16 kHz before upload. The accuracy won't be perfect since you can't recover information that wasn't captured, but it's significantly better than rejection."},
            {"speaker_id": "customer", "speaker_name": "Ingrid Svensson", "sentiment": "neutral",
             "text": "Can your team do the conversion for us?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "That's outside our standard support scope but I can loop in our professional services team for a quote if you have a large volume. How many files are um we talking?"},
            {"speaker_id": "customer", "speaker_name": "Ingrid Svensson", "sentiment": "neutral",
             "text": "About 4,000 recordings."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "At that volume, professional services makes sense. I'll connect you with them — they can often turn around batch conversions pretty quickly. I'll send you both the ffmpeg script if you wanna try a few yourself and the intro to our PS team."},
            {"speaker_id": "customer", "speaker_name": "Ingrid Svensson", "sentiment": "positive",
             "text": "Yes please. That's helpful. I appreciate you explaining the technical side."},
        ],
    },
    {
        "title": "404 on Transcript Export Endpoint — API Integration Bug",
        "agent_email": "sam.patel@callsight.ai",
        "customer_name": "Felix Hartmann",
        "customer_company": "MediConnect Solutions",
        "customer_title": "Software Developer",
        "duration_secs": 1080,
        "source": "video",
        "days_ago": 60,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Hey Felix, Sam Patel from CallSight tech support. You filed a ticket about a 404 on the export endpoint?"},
            {"speaker_id": "customer", "speaker_name": "Felix Hartmann", "sentiment": "negative",
             "text": "Yeah. From what I remember, we're hitting GET /api/v2/calls/{id}/transcript/export and getting a 404 every time, even with valid call IDs."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Can you share the full URL you're using and an example call ID so I can test it on my end?"},
            {"speaker_id": "customer", "speaker_name": "Felix Hartmann", "sentiment": "neutral",
             "text": "Sure. https://api.callsight.ai/api/v2/calls/abc123/transcript/export. The call ID is definitely valid — I can see the call in the dashboard."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Makes sense. Found it. The export endpoint in v2 moved — it's now /api/v2/calls/{id}/export/transcript, not /transcript/export. I think the path segments are reversed. This is a breaking change from v1 that wasn't clearly marked in the migration guide."},
            {"speaker_id": "customer", "speaker_name": "Felix Hartmann", "sentiment": "negative",
             "text": "That's a significant oversight. We built our entire pipeline around the v2 docs."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Okay, so, you're absolutely right and I apologize for that. I'm gonna escalate this to our API team — we should either redirect the old path or correct the documentation immediately. In the meantime, updating your code to use the new path will unblock you."},
            {"speaker_id": "customer", "speaker_name": "Felix Hartmann", "sentiment": "neutral",
             "text": "What's the timeline on the fix?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "I can't commit to a specific date, but I'll file this as a high-priority documentation bug and request a redirect be added to the API. I'll follow up on your ticket once it's resolved."},
            {"speaker_id": "customer", "speaker_name": "Felix Hartmann", "sentiment": "neutral",
             "text": "Fine. I'll update our code. But please make sure this doesn't happen again in v3 — we need to be able to trust the docs."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "Um, completely fair. I'll add your feedback to the escalation note. Thanks for flagging this, Felix."},
        ],
    },
    {
        "title": "Zoom Integration Setup — Recording Auto-Import",
        "agent_email": "casey.rivera@callsight.ai",
        "customer_name": "Tanya Brooks",
        "customer_company": "Horizon Telecom",
        "customer_title": "Customer Experience Director",
        "duration_secs": 1980,
        "source": "video",
        "days_ago": 55,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Casey Rivera, integrations. Hey Tanya! Ready to get your Zoom recordings flowing into CallSight?"},
            {"speaker_id": "customer", "speaker_name": "Tanya Brooks", "sentiment": "positive",
             "text": "Yes! Our team is really excited about this. We have about 200 customer calls per week on Zoom and manually uploading is killing us."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Right, right. The auto-import will take care of so that completely. Here's how it works: we use Zoom's webhook for recording completed events, pull the file from Zoom's cloud, process it through our pipeline, and it shows up in CallSight within about 5 minutes of the call ending."},
            {"speaker_id": "customer", "speaker_name": "Tanya Brooks", "sentiment": "positive",
             "text": "Um, that's perfect. What do I need basically to do on the Zoom side?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Yeah. You'll need Zoom admin access. Go to the Zoom App Marketplace, find the CallSight app, and install it. During installation it'll ask for permission to access cloud recordings — approve that. Then come back to CallSight and go to Admin > Integrations > Zoom... right?"},
            {"speaker_id": "customer", "speaker_name": "Tanya Brooks", "sentiment": "neutral",
             "text": "Okay, I'm in the Zoom Marketplace. I see the CallSight app. Installing now... it's asking for recording permissions and meeting host data. Approving. Okay, it says installed... you know what I mean?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Great. Now in CallSight — Admin > Integrations > Zoom. You should see a green 'Connected' status."},
            {"speaker_id": "customer", "speaker_name": "Tanya Brooks", "sentiment": "positive",
             "text": "I see it — Connected. Now what?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Okay, so, configure which meetings to import. You can do all meetings, or filter by host or meeting topic keyword. I'd recommend starting with all recordings from specific hosts — your sales team maybe — and expanding from there."},
            {"speaker_id": "customer", "speaker_name": "Tanya Brooks", "sentiment": "neutral",
             "text": "Right, so, let's start with the customer success team. Can I filter by Zoom user group?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Not by group directly, but you can add individual host emails. Once you add them, any recording they host will auto-import."},
            {"speaker_id": "customer", "speaker_name": "Tanya Brooks", "sentiment": "neutral",
             "text": "Got it. I've added the six CS team hosts. What about past recordings?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "You can trigger a one-time backfill from Admin > Integrations > Zoom > Import History. It'll pull up to 90 days of existing cloud recordings [laughs] . Fair warning — if you have a lot, it queues them and processes over an hour or two so it doesn't overwhelm the pipeline."},
            {"speaker_id": "customer", "speaker_name": "Tanya Brooks", "sentiment": "positive",
             "text": "Starting the backfill now. This is going to save my team so much time. Thank you Casey."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Happy to help. You'll get an email when the backfill completes. Your team should be seeing calls in the dashboard by end of day."},
        ],
    },
    {
        "title": "HIPAA Compliance Audit — PII Redaction Verification",
        "agent_email": "sam.patel@callsight.ai",
        "customer_name": "Dr. Renata Okafor",
        "customer_company": "MediConnect Solutions",
        "customer_title": "Chief Compliance Officer",
        "duration_secs": 2520,
        "source": "video",
        "days_ago": 49,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Hi Dr. Okafor, Sam Patel from CallSight technical support. I understand you're conducting a HIPAA compliance review of our platform?"},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "That's correct. We're a covered entity and before we expand our CallSight usage to include patient-facing call lines, our legal team requires verification of your PII handling and redaction capabilities."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Absolutely. Let me walk you through our data handling architecture. All audio and transcripts are encrypted at rest with AES-256 and in transit with TLS 1.3. Our PII redaction engine runs on every transcript before storage."},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "What categories of PII does your redaction engine cover?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Names, phone numbers, email addresses, dates of birth, credit card numbers, and street addresses. These are automatically detected and replaced with bracketed placeholders in the stored transcript."},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "negative",
             "text": "What about Social Security numbers? For our use case, patients sometimes verbally provide SSNs for identity verification. I believe that's highly regulated PHI."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "negative",
             "text": "Yeah, so, that's a critical question. I wanna be honest with you — SSN redaction is on our roadmap but isn't currently in our default PII redaction suite. I shouldn't have implied otherwise. I need to escalate this."},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "negative",
             "text": "Honestly, this is a significant problem. If SSNs are being stored in plain text in transcripts, that's a HIPAA violation waiting to happen. We cannot expand to patient lines with this gap."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Mhm. I completely understand and I'm um treating this as a critical issue. I'm going to escalate immediately to our VP of Engineering and our security team. I want to be transparent — I will get you a written response within 24 hours outlining our immediate mitigation plan and committed timeline for SSN redaction."},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "I'll need that in writing for our legal team. We're also going to need to audit any transcripts that have already been stored."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "We can facilitate a transcript audit for your tenant's data. That will require a formal data access request from your legal team. I'll include the process for that in my written response as well... you know what I mean? [chuckles] "},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "Alright. I'm not happy about this but I appreciate that you were direct with me rather than trying to cover it up. I'll look for your written response."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "You have my commitment. I'll send it today [chuckles] . This is being treated with uh the highest priority."},
        ],
    },
    {
        "title": "Multi-Tenant White-Label Configuration — Custom Domain Setup",
        "agent_email": "casey.rivera@callsight.ai",
        "customer_name": "Priya Mehta",
        "customer_company": "Horizon Telecom",
        "customer_title": "Product Manager",
        "duration_secs": 2280,
        "source": "video",
        "days_ago": 43,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Casey Rivera, CallSight integrations. Hey Priya! Excited to get your white-label portal set up today."},
            {"speaker_id": "customer", "speaker_name": "Priya Mehta", "sentiment": "positive",
             "text": "Same here. We're planning to roll this out as 'Horizon Insights' to our enterprise customers. I need the portal to run on insights.horizontelecom.com with our branding throughout... you know what I mean?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Perfect. Custom domain setup has three parts: DNS configuration, SSL provisioning, and branding customization. Let's do DNS first. You'll need to add a CNAME record pointing insights.horizontelecom.com to your-tenant.callsight.ai."},
            {"speaker_id": "customer", "speaker_name": "Priya Mehta", "sentiment": "neutral",
             "text": "What's our tenant subdomain?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Right, so, it's horizon-telecom.callsight.ai. Add the CNAME from insights.horizontelecom.com to horizon-telecom.callsight.ai and propagation usually takes 30 to 60 minutes."},
            {"speaker_id": "customer", "speaker_name": "Priya Mehta", "sentiment": "neutral",
             "text": "I mean, i'll have our DNS admin do that now. What about SSL?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Once DNS propagates, go to Admin > White Label > Custom Domain, enter insights.horizontelecom.com, and click 'Provision SSL'. We use Let's Encrypt so it's automatic — takes about two minutes."},
            {"speaker_id": "customer", "speaker_name": "Priya Mehta", "sentiment": "positive",
             "text": "That's much simpler than I expected. Now branding — we've strict brand guidelines. Can we control fonts, colors, and logos?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Full control. In the White Label settings you can upload your logo, favicon, set primary and secondary brand colors as hex codes, and choose from a set of approved fonts. If you need a custom font not on the list, we can add it — just send us the web font files."},
            {"speaker_id": "customer", "speaker_name": "Priya Mehta", "sentiment": "neutral",
             "text": "We use Circular as our brand font. It's not a free font — can you handle licensing?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Mhm. Well, your license covers use in your product, so you'd provide the font files and we host them within your tenant's CDN allocation. Send them over and we'll have it configured within a day."},
            {"speaker_id": "customer", "speaker_name": "Priya Mehta", "sentiment": "neutral",
             "text": "One more thing — can we remove all CallSight branding? Our customers shouldn't know this is built on CallSight."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Fully supported. In White Label settings there's a 'Complete Brand Removal' toggle. That removes our logo from the UI, email footers, and report PDFs. The only place CallSight appears after that is in legal agreements, which your team controls anyway."},
            {"speaker_id": "customer", "speaker_name": "Priya Mehta", "sentiment": "positive",
             "text": "Perfect. That's exactly what we need. I'll get our DNS admin on the CNAME and send you the font files today."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Right, so, sounds great. Once DNS um propagates, ping me and we'll finish the setup together. Your Horizon Insights portal will be live before end of week."},
        ],
    },
    {
        "title": "Real-Time Transcription Latency — Delay Exceeding 8 Seconds",
        "agent_email": "sam.patel@callsight.ai",
        "customer_name": "Oscar Mejia",
        "customer_company": "ProServe BPO",
        "customer_title": "Contact Center Manager",
        "duration_secs": 1680,
        "source": "phone",
        "days_ago": 37,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Yeah, so, technical support, Sam Patel. What's going on today?"},
            {"speaker_id": "customer", "speaker_name": "Oscar Mejia", "sentiment": "negative",
             "text": "Hi Sam. Our agents are seeing an 8-to-12 second delay in the real-time transcription display. That makes it essentially useless — supervisors can't coach in real time if they're reading something that happened 10 seconds ago... right?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "I hear you — that latency completely defeats the purpose. A few diagnostic questions: what's your contact center's geographic region and are your agents using the web interface or our desktop app?"},
            {"speaker_id": "customer", "speaker_name": "Oscar Mejia", "sentiment": "neutral",
             "text": "We're in Phoenix, agents are all on the web interface, Chrome specifically."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Phoenix routes to our US-West endpoint. Can you tell me the audio source — are these Twilio calls, your own SIP trunking, or something else?"},
            {"speaker_id": "customer", "speaker_name": "Oscar Mejia", "sentiment": "neutral",
             "text": "Our own SIP trunking through a third-party carrier. The audio streams to CallSight via your WebSocket API."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "That's where the latency likely is. Can you check what audio chunk size you're sending? If you're buffering 3 to 4 seconds of audio before sending each chunk, that explains the delay right there."},
            {"speaker_id": "customer", "speaker_name": "Oscar Mejia", "sentiment": "neutral",
             "text": "I'll need to check with our dev team. I'm pretty sure what should it be?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "For real-time use, send 500-millisecond chunks or smaller. The model processes incrementally — smaller chunks mean faster display. Most of our clients doing real-time end up at 250ms to 500ms."},
            {"speaker_id": "customer", "speaker_name": "Oscar Mejia", "sentiment": "neutral",
             "text": "Our dev says they're sending 4-second chunks. So that's it."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "Um, that explains your 8 to 12 second lag — 4-second buffer plus processing time. Drop to 500ms chunks and you should see the display latency fall under 2 seconds. If I'm not mistaken, i'll also note your account for priority processing on the US-West node, which shaves off another few hundred milliseconds."},
            {"speaker_id": "customer", "speaker_name": "Oscar Mejia", "sentiment": "positive",
             "text": "Let me have the dev make that change now. If it works, this is going to be a game-changer for our supervisors."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "Um, i'll stay on. Have them try a test call once the change is deployed."},
            {"speaker_id": "customer", "speaker_name": "Oscar Mejia", "sentiment": "positive",
             "text": "Alright, so, testing... wow. The display is basically live now. That's incredible. Okay, great work Sam."},
        ],
    },
    {
        "title": "Bulk Upload via API — Automating Historical Recording Import",
        "agent_email": "casey.rivera@callsight.ai",
        "customer_name": "Michael Osei",
        "customer_company": "BrightPath Financial",
        "customer_title": "CRM Administrator",
        "duration_secs": 1440,
        "source": "video",
        "days_ago": 31,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Casey Rivera, integrations support. Hey Michael, what are you trying to accomplish today?"},
            {"speaker_id": "customer", "speaker_name": "Michael Osei", "sentiment": "neutral",
             "text": "We have about 18 months of call recordings sitting in an S3 bucket — roughly 12,000 files. I need to get them all into CallSight without someone manually uploading each one."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "That's exactly what our — well, my team's bulk ingest API is built for. You can POST to /api/v2/ingest/batch with a JSON body containing an array of S3 URLs and metadata. We'll pull the files directly from S3... right?"},
            {"speaker_id": "customer", "speaker_name": "Michael Osei", "sentiment": "neutral",
             "text": "Do we need to make the S3 bucket public?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "No, keep it private. You generate presigned S3 URLs with a 2-hour expiry and pass those to us. Our ingest service downloads within that window. Much more secure than making the bucket public."},
            {"speaker_id": "customer", "speaker_name": "Michael Osei", "sentiment": "neutral",
             "text": "What metadata do we need per file?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Required: the audio URL, call_date, and call_type. Optional but useful: agent_email, duration_seconds, customer_phone, and any custom metadata fields you want to preserve. If you have a CSV with your recording inventory, you can map columns to our schema pretty easily."},
            {"speaker_id": "customer", "speaker_name": "Michael Osei", "sentiment": "neutral",
             "text": "We have a CSV. What's the batch size limit?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "500 records per API call. For 12,000 files like that's 24 batches. You can send them sequentially or parallelize up to 5 concurrent batch jobs. Each job returns a job ID you can poll for completion status."},
            {"speaker_id": "customer", "speaker_name": "Michael Osei", "sentiment": "positive",
             "text": "This is really straightforward. One question — will all 12,000 go through AI analysis immediately or is there a queue?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Got it. They queue and process in order. With 12,000 files, expect the full batch to complete in 6 to 8 hours depending on average call length. Shorter calls process faster. You'll get a webhook notification when the full batch I mean is done."},
            {"speaker_id": "customer", "speaker_name": "Michael Osei", "sentiment": "positive",
             "text": "Perfect. I'll start building the  — sorry, someone just walked in —  ingestion script this week. Can you share the API spec for you know the batch endpoint?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Sending you the link now. Reach out if you hit any snags — I can do a code review of your ingestion script if that would help."},
        ],
    },
    {
        "title": "BI Tool Data Connector — Connecting CallSight to Power BI",
        "agent_email": "sam.patel@callsight.ai",
        "customer_name": "Yuki Tanaka",
        "customer_company": "CoreTech Systems",
        "customer_title": "Business Intelligence Manager",
        "duration_secs": 1800,
        "source": "video",
        "days_ago": 26,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Well, sam Patel, technical support. Hi Yuki — you're looking to connect Power BI to CallSight data?"},
            {"speaker_id": "customer", "speaker_name": "Yuki Tanaka", "sentiment": "neutral",
             "text": "Yes. Our analytics team wants to build custom dashboards combining CallSight metrics with our CRM and ERP data in Power BI. I need to understand what options we have."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Three options depending on your preference. One — our REST API, which Power BI can query via a custom connector. Two — our direct database read replica that Power BI can connect to via ODBC. Three — our Power BI certified connector available in the Power BI App Marketplace, which is the easiest."},
            {"speaker_id": "customer", "speaker_name": "Yuki Tanaka", "sentiment": "positive",
             "text": "I didn't know about — well, roughly the certified connector. That does sound easiest. What data does it expose?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Calls, transcripts, AI insights, action items, agent performance scores, sentiment trends over time, and topic frequency. All queryable by date range, agent, call type, and outcome. The connector refreshes on a schedule you configure — down to 15-minute intervals."},
            {"speaker_id": "customer", "speaker_name": "Yuki Tanaka", "sentiment": "positive",
             "text": "That covers what we need. How do we authenticate from Power BI to CallSight?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "OAuth 2.0. When you set up the connector in Power BI, it'll open a CallSight login prompt. You authenticate once and Power BI stores the token. For automated refresh, you'll use a service account — I'd recommend creating one in Admin > Users with read-only data access. [laughs] "},
            {"speaker_id": "customer", "speaker_name": "Yuki Tanaka", "sentiment": "neutral",
             "text": "Can the service account access all tenants or just ours?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Just yours — tenant data isolation is enforced at the API layer regardless of credentials. No cross-tenant access is possible by design."},
            {"speaker_id": "customer", "speaker_name": "Yuki Tanaka", "sentiment": "positive",
             "text": "Good. One thing our team wants to do is combine call topics from CallSight with deal stage from our CRM. Is there a way to pass a CRM deal ID through CallSight so we can join the data?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "Yes — our call records support a custom_metadata JSONB field. You can push a CRM deal ID when the call is created via the API or Twilio integration. Then in Power BI, that field is exposed as a column you can use for joins."},
            {"speaker_id": "customer", "speaker_name": "Yuki Tanaka", "sentiment": "positive",
             "text": "Okay, so, that's exactly what we need. I'll get our dev team to start passing the deal ID in the call metadata. Thank you, Sam — this gives us everything we need to build the dashboard."},
        ],
    },
    {
        "title": "Critical Bug — PII Redaction Missing SSNs in Stored Transcripts",
        "agent_email": "sam.patel@callsight.ai",
        "customer_name": "Dr. Renata Okafor",
        "customer_company": "MediConnect Solutions",
        "customer_title": "Chief Compliance Officer",
        "duration_secs": 1920,
        "source": "phone",
        "days_ago": 20,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Right, so, dr. Okafor, Sam Patel from CallSight. I'm calling with an update on the SSN redaction issue we identified during your compliance review."},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "I've been waiting for this call. Your 24-hour written response arrived but I've questions."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Of course. What are your questions?"},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "negative",
             "text": "Your written response said SSN redaction will be deployed in the 'next release.' That's not a commitment. I believe our legal team needs a specific date and confirmation of what has been done for you know existing stored transcripts."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Right, so, i understand. I'm authorized to share the specific timeline today. SSN pattern matching has been developed and is in QA now. Deployment is scheduled for this Friday — three days from today. That covers all new transcripts going forward... you know what I mean?"},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "And for existing transcripts?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "We've run a scan of all transcripts in your tenant. We found 14 instances across 9 calls where SSNs appear in stored transcripts — all from the last 6 months. We've redacted those 14 instances and the remediation is complete as of this morning."},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "Well, i'll need a written attestation of that remediation for our compliance files. And were any of those transcripts accessed by unauthorized users?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Our access logs show the 9 affected transcripts were accessed only by 3 users within your own tenant — your internal team. No external access. I'll include the access log summary in the written attestation."},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "I mean, good. As far as I know, what's your process for ensuring this doesn't recur? I wanna know what systematic change is being made."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "So, three changes: first, the SSN pattern is being added to our PII detection suite permanently. From what I remember, second, we're adding a nightly automated scan that flags any transcript where high-confidence PII escapes redaction. Third, we're updating our PII coverage documentation to explicitly list what we redact and what we don't — that kind of gap is how this happened."},
            {"speaker_id": "customer", "speaker_name": "Dr. Renata Okafor", "sentiment": "neutral",
             "text": "Alright, so, i appreciate the transparency and the thoroughness of the response. This is how compliance issues should be handled. I'll share this with our legal team."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "I'll send the written attestation within two hours. And Dr. Okafor — thank you for catching this. It made our platform better."},
        ],
    },
    {
        "title": "Agent Login Issues — Locked Accounts After SSO Rollout",
        "agent_email": "casey.rivera@callsight.ai",
        "customer_name": "Jordan Reyes",
        "customer_company": "BrightPath Financial",
        "customer_title": "Helpdesk Lead",
        "duration_secs": 1200,
        "source": "phone",
        "days_ago": 14,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Um, casey Rivera, CallSight support. What's the situation?"},
            {"speaker_id": "customer", "speaker_name": "Jordan Reyes", "sentiment": "negative",
             "text": "We just rolled out SSO last week and now 12 of our agents can't log in. They're getting 'account not found' errors when they authenticate through Okta."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "I see the pattern. When SSO is enabled, the system expects to match Okta user emails to CallSight accounts. If the email in Okta doesn't exactly match the email on the CallSight account, login fails."},
            {"speaker_id": "customer", "speaker_name": "Jordan Reyes", "sentiment": "neutral",
             "text": "How do I check? I need to get these 12 people back in — they've been locked out all morning."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "In CallSight Admin > Users, export the user list as CSV. Compare the email column to what you have in Okta. Any mismatches will be your issue. Common ones are people who changed their last name and updated Okta but not CallSight."},
            {"speaker_id": "customer", "speaker_name": "Jordan Reyes", "sentiment": "neutral",
             "text": "Exporting now... yeah, I see it. Several accounts show old email addresses — maiden names. And a few have a different domain — some are @brightpathfinancial.com and others are @brightpath.com."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Okay, so, update the email on the CallSight accounts to match Okta exactly. That'll restore access immediately — no password reset, no re-provisioning. The SSO um match is purely on email."},
            {"speaker_id": "customer", "speaker_name": "Jordan Reyes", "sentiment": "neutral",
             "text": "Can I bulk update or does it have to be one by one?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Admin > Users > Import lets you upload a CSV with email updates. I think update the email column in your export and re-import — it'll match on user ID and update the email field."},
            {"speaker_id": "customer", "speaker_name": "Jordan Reyes", "sentiment": "positive",
             "text": "Doing it now. Uploading CSV... processed. Testing a login for one of the affected users... they're in. Great. That was fast... so yeah."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "For future SSO rollouts, worth doing the email comparison before you enable SSO rather than after. I can share a pre-rollout checklist that includes that step."},
            {"speaker_id": "customer", "speaker_name": "Jordan Reyes", "sentiment": "positive",
             "text": "Please send that. Would have saved us this morning. Thanks Casey."},
        ],
    },
    {
        "title": "Webhook Signature Validation Failure — Security Headers",
        "agent_email": "sam.patel@callsight.ai",
        "customer_name": "Alex Torres",
        "customer_company": "CoreTech Systems",
        "customer_title": "Backend Developer",
        "duration_secs": 1320,
        "source": "phone",
        "days_ago": 9,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Technical support, Sam speaking. What's the issue today?"},
            {"speaker_id": "customer", "speaker_name": "Alex Torres", "sentiment": "neutral",
             "text": "Hey Sam. We're implementing webhook signature validation — we wanna verify that events from CallSight are actually from you and not spoofed. The signature in the header never matches what we compute."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Good security practice. Let me walk you through the signing algorithm. We use HMAC-SHA256. The signature is computed over the raw request body concatenated with the timestamp header. What are you signing?"},
            {"speaker_id": "customer", "speaker_name": "Alex Torres", "sentiment": "neutral",
             "text": "Just the request body. We parse it to JSON first and then compute the HMAC."},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "Sure, sure. That's the issue. You need to sign the raw body bytes, not the parsed JSON. When you serialize a JSON object, key order can change and whitespace handling differs — the bytes won't match what we signed. Read the raw request body from the HTTP stream before any JSON parsing."},
            {"speaker_id": "customer", "speaker_name": "Alex Torres", "sentiment": "neutral",
             "text": "And the timestamp — where does that go in the signature computation?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "neutral",
             "text": "The format is: HMAC-SHA256 of the string 'v1:' plus the Unix timestamp from the X-CallSight-Timestamp header, plus a I mean colon, plus the raw body bytes. The secret is your webhook signing secret from Admin > Webhooks."},
            {"speaker_id": "customer", "speaker_name": "Alex Torres", "sentiment": "neutral",
             "text": "Let me try that... okay, signatures match now. But one more question — should we reject webhooks where the timestamp is too old?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "Makes sense. Yes, absolutely. That's a replay attack protection measure. Reject any webhook where  — hold on one sec — okay, I'm back —  the timestamp is more than 5 minutes old. We include the timestamp for exactly that reason... so yeah."},
            {"speaker_id": "customer", "speaker_name": "Alex Torres", "sentiment": "positive",
             "text": "Good call. I'll add that check. The signature validation is working now. Thanks for the clear explanation — the docs were a bit vague on the concatenation format... right?"},
            {"speaker_id": "agent", "speaker_name": "Sam Patel", "sentiment": "positive",
             "text": "Okay, so, i'll flag that to the docs team. You're not the first person to hit this — the 'v1:timestamp:body' format needs a code example in the docs [chuckles] . Glad it's working."},
        ],
    },
    {
        "title": "Data Retention Policy Configuration — GDPR Deletion Requests",
        "agent_email": "casey.rivera@callsight.ai",
        "customer_name": "Sophie Dubois",
        "customer_company": "BrightPath Financial",
        "customer_title": "Data Privacy Manager",
        "duration_secs": 1560,
        "source": "video",
        "days_ago": 4,
        "segments": [
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Casey Rivera, CallSight. Hi Sophie — I understand this is about GDPR data deletion and retention settings?"},
            {"speaker_id": "customer", "speaker_name": "Sophie Dubois", "sentiment": "neutral",
             "text": "Okay, so, yes. We've received a right-to-erasure request from a customer whose calls were recorded through BrightPath's contact center. I need to delete all associated recordings and transcripts from CallSight. How do we do that?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "We have a subject data deletion tool for exactly this. You can search by customer phone number or a name pattern. It will identify all calls and transcripts associated with that individual and delete both the audio files and transcript data permanently."},
            {"speaker_id": "customer", "speaker_name": "Sophie Dubois", "sentiment": "neutral",
             "text": "Does deletion cascade to AI insights and action items too?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Okay, so, yes, full cascade. Deleting a call removes the audio file from storage, the transcript, transcript segments, all AI insights, and all action items. Nothing orphaned. You get a deletion confirmation receipt that's audit-ready."},
            {"speaker_id": "customer", "speaker_name": "Sophie Dubois", "sentiment": "neutral",
             "text": "Good. What about backups? If we've a backup retention policy, can those backups also contain this individual's data?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "Our backups are encrypted and held for 30 days. Subject deletion requests are flagged in our system so that when a backup is restored for any reason, the deletion is re-applied before any data becomes accessible. We don't restore individual records from backup — it's all or nothing, and GDPR flags always override."},
            {"speaker_id": "customer", "speaker_name": "Sophie Dubois", "sentiment": "neutral",
             "text": "That's the right design. Now, separately — we wanna set an automatic data retention policy. Under GDPR we shouldn't be holding recordings more than 12 months."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "neutral",
             "text": "In Admin > Data Management > Retention Policy, you can set automatic deletion by data type and age. You can configure audio files to delete after 12 months while retaining transcripts, or delete everything. You can also exclude specific call types — some clients keep compliance recordings longer."},
            {"speaker_id": "customer", "speaker_name": "Sophie Dubois", "sentiment": "neutral",
             "text": "For us it's 12 months for everything except flagged compliance calls which we hold for 7 years. Can we tag calls as compliance holds?"},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Yes — there's a 'Legal Hold' flag on each call that exempts it from automatic retention deletion. You can apply it manually or automatically via a rule — for example, any call tagged with a compliance topic by the AI... does that make sense?"},
            {"speaker_id": "customer", "speaker_name": "Sophie Dubois", "sentiment": "positive",
             "text": "That handles both cases. I'm pretty sure let me set the retention policy now... done. 12 months, legal hold exemption enabled. And I'll handle the GDPR deletion manually for this request."},
            {"speaker_id": "agent", "speaker_name": "Casey Rivera", "sentiment": "positive",
             "text": "Yeah. So, you're all set. The deletion tool is in Admin > Data Management > Subject Deletion. Keep the receipt it generates — it includes a timestamp and a hash of the deleted records for your audit trail."},
            {"speaker_id": "customer", "speaker_name": "Sophie Dubois", "sentiment": "positive",
             "text": "Excellent. This covers everything I needed. Thank you Casey."},
        ],
    },
]


# ── Insert IT support calls ───────────────────────────────────────────────────
def insert_it_calls(conn, tenant_id, user_map):
    inserted = 0
    total_segments = 0

    with conn.cursor() as cur:
        for call in IT_CALLS:
            agent_uid = user_map[call["agent_email"]]
            call_id = new_id()
            transcript_id = new_id()
            created_at = days_ago(call["days_ago"])

            participants = [
                {
                    "name": call["customer_name"],
                    "role": "customer",
                    "title": call["customer_title"],
                    "company": call["customer_company"],
                },
                {
                    "name": call["agent_email"].split("@")[0].replace(".", " ").title(),
                    "role": "agent",
                },
            ]

            # ── call row ───────────────────────────────────────────────────
            cur.execute(
                """
                INSERT INTO calls
                    (id, tenant_id, uploaded_by, title, call_type, participants,
                     duration_secs, source, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    call_id, tenant_id, agent_uid,
                    call["title"], "it_support",
                    json.dumps(participants),
                    call["duration_secs"], call["source"], "complete",
                    created_at, created_at,
                ),
            )

            # ── transcript ─────────────────────────────────────────────────
            full_text = build_full_text(call["segments"])
            word_count = len(full_text.split())
            cur.execute(
                """
                INSERT INTO transcripts
                    (id, call_id, tenant_id, full_text, word_count, engine)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (transcript_id, call_id, tenant_id, full_text, word_count, "whisper"),
            )

            # ── segments ───────────────────────────────────────────────────
            start_ms = 0
            for idx, seg in enumerate(call["segments"]):
                seg_id = new_id()
                end_ms = ms_from_words(seg["text"], start_ms)
                cur.execute(
                    """
                    INSERT INTO transcript_segments
                        (id, transcript_id, speaker_id, speaker_name,
                         text, start_ms, end_ms, sentiment, seq_order)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        seg_id, transcript_id,
                        seg["speaker_id"], seg["speaker_name"],
                        seg["text"], start_ms, end_ms,
                        seg["sentiment"], idx,
                    ),
                )
                start_ms = end_ms
                total_segments += 1

            inserted += 1

    return inserted, total_segments


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Connecting to Neon (Flex)...")
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    print("✓ Connected\n")

    tenant_id, user_map = fetch_tenant_and_users(conn)
    print(f"✓ Tenant found: {tenant_id}")
    print(f"✓ Users loaded: {sorted(user_map.keys())}\n")

    n_calls, n_segs = insert_it_calls(conn, tenant_id, user_map)
    conn.commit()

    # ── Summary ───────────────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT call_type, COUNT(*) FROM calls GROUP BY call_type ORDER BY call_type")
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM transcript_segments")
        total_segs = cur.fetchone()[0]

    print(f"✓ {n_calls} IT support calls inserted ({n_segs} transcript segments)")
    for call_type, count in rows:
        print(f"  {call_type}: {count} calls")
    print(f"  total segments: {total_segs}")
    print("\n✓ Done.")
    conn.close()


if __name__ == "__main__":
    main()
