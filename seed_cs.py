#!/usr/bin/env python3
# SYNTHETIC DATA WARNING
# Every name, email address, phone number, company, and transcript below is
# fictional. Any resemblance to a real person or organization is coincidental.
# Do NOT treat the contents of this file as customer data.
"""
CallSight AI — Customer Service Call Transcript Seed Script
Appends 17 realistic CS call transcripts to the existing Flex database.
Schema, tenant, and users are assumed to already exist (run seed_sales.py first).
"""

import os
import uuid
import json
import random
import psycopg2
from datetime import datetime, timedelta

env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

DB_URL = os.environ["DATABASE_URL"]

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

# ── CS call data ──────────────────────────────────────────────────────────────

CS_CALLS = [

    {
        "title": "Sentiment Export Feature — Horizon Telecom",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Rosa Ingram",
        "customer_company": "Horizon Telecom",
        "customer_title": "Operations Analyst",
        "duration_secs": 840,
        "source": "phone",
        "days_ago": 5,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Thanks for calling CallSight support, this is Aisha. How can I help you today?"},
            {"speaker_id": "customer", "speaker_name": "Rosa Ingram",    "sentiment": "neutral",
             "text": "I mean, hi, I'm trying to export sentiment data for our monthly report and I cannot for the life of me find where that option is. I've looked all through so the dashboard."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Totally understandable — it's a bit tucked away right now. Are you in I mean the Insights tab or the Calls view?"},
            {"speaker_id": "customer", "speaker_name": "Rosa Ingram",    "sentiment": "neutral",
             "text": "I've been in both. I can see the sentiment scores on each call but I can't find a way to pull them all into a CSV or spreadsheet."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Got it. So the bulk export lives under Settings, then Data Exports — it's not on the main dashboard which I know is counterintuitive. Once you're there you can set a date range, choose which fields to include — sentiment score is one of them — and it generates a CSV download. Want me to walk you through uh it step by step?"},
            {"speaker_id": "customer", "speaker_name": "Rosa Ingram",    "sentiment": "positive",
             "text": "Yes please. Okay I'm in Settings now."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Right, so, great. You should see Data Exports in the left menu. Click that, then New Export. Set your date range to whatever you need for the monthly report. Then under Fields, scroll down to Call Metrics — sentiment score and sentiment label are both in there. Check those, hit Generate, and it'll email you the download link within a minute or two."},
            {"speaker_id": "customer", "speaker_name": "Rosa Ingram",    "sentiment": "positive",
             "text": "Oh perfect, I can see it now. Yeah I would never have found this on my own. This is great."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Yeah, so, i'll make a note to flag this in our onboarding materials — you're not the first person to ask. Is there anything else I can help you with today?"},
            {"speaker_id": "customer", "speaker_name": "Rosa Ingram",    "sentiment": "positive",
             "text": "No, that's it. Really appreciate the quick help."},
        ],
    },

    {
        "title": "Salesforce Sync Failure — BrightPath Financial",
        "agent": "derek.liu@callsight.ai",
        "customer_name": "Owen Cartwright",
        "customer_company": "BrightPath Financial",
        "customer_title": "Sales Operations Manager",
        "duration_secs": 2160,
        "source": "phone",
        "days_ago": 8,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",        "sentiment": "positive",
             "text": "CallSight support, Derek speaking. What can I help you with?"},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright",  "sentiment": "negative",
             "text": "Hi Derek. Our Salesforce integration has stopped syncing. Our reps are logging calls manually again because nothing is pushing through, and they are not happy about it. This has been broken for two days."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",        "sentiment": "positive",
             "text": "I'm sorry to hear that — two days of manual logging is painful. Let me pull up your account right now. I think brightPath Financial, is that right?"},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright",  "sentiment": "neutral",
             "text": "That's correct. It was working perfectly for months and then two days ago it just stopped. No error messages on our side, nothing obvious."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",        "sentiment": "positive",
             "text": "Okay, I can see your account. Let me check the integration logs. Can you tell me — did anything change in your Salesforce environment around the same time? A new deployment, permission update, anything like that?"},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright",  "sentiment": "neutral",
             "text": "Actually — yes. Our Salesforce admin pushed a release two days ago. New permission sets for a compliance project. Could that have affected the connected app permissions?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",        "sentiment": "positive",
             "text": "That is almost certainly the cause. When permission sets change, the OAuth token our integration uses can have its scope silently revoked. I can see in your logs that we're getting a 403 Forbidden on the activity write endpoint — that's a permissions error, not a connectivity issue [chuckles] . The fix is to re-authorize the connected app from your Salesforce side. Do you have admin access basically in Salesforce, or do I need to walk you through getting your admin involved?"},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright",  "sentiment": "neutral",
             "text": "I can get our admin on a call. What exactly does she need to do?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",        "sentiment": "positive",
             "text": "She needs to go to Connected Apps in Salesforce setup, find the CallSight integration, and check that the OAuth scopes include API access and refresh token. From what I remember, then from our side you'll need to go to Settings, Integrations, Salesforce, and click Re-authorize. That will issue a fresh token with the correct permissions. The whole thing takes about five minutes."},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright",  "sentiment": "positive",
             "text": "Okay, I'm so texting her right now. Can you stay on the line while we do this?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",        "sentiment": "positive",
             "text": "Absolutely. Take your time."},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright",  "sentiment": "positive",
             "text": "Okay she's done it. I'm re-authorizing from our dashboard now. And — yes, I can see a test record just pushed through. It's working."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",        "sentiment": "positive",
             "text": "Yeah, so, excellent. I'm gonna add a note to your account and also flag this with our product team — we should be showing a clearer error message when this happens rather than just silently failing. That's on us."},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright",  "sentiment": "positive",
             "text": "I agree, a better error message would have saved two days of frustration. But I appreciate you diagnosing it so quickly. Thank you."},
        ],
    },

    {
        "title": "New Team Member Onboarding — MediConnect Solutions",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Claire Nguyen",
        "customer_company": "MediConnect Solutions",
        "customer_title": "Patient Services Manager",
        "duration_secs": 1980,
        "source": "zoom",
        "days_ago": 12,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Claire, great to connect for your onboarding session. I see you have three new team members being added — do you want to walk through the admin setup together or do you need the refresher on the dashboard itself too?"},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "positive",
             "text": "Mainly the admin setup. I know how to use it myself but I want to make sure I get their roles and permissions right. We have two supervisors and one quality analyst."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Perfect. So supervisors should probably get the Manager role — that gives them access to the team performance view and individual call coaching notes for their direct reports, but doesn't give them access to billing or account settings. Does that sound right for how your supervisors are structured?"},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "positive",
             "text": "Yes, exactly. They should be able to see their you know team's calls and coach, but I don't want them in billing."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "And for the quality analyst — their job is probably reviewing calls across multiple teams, not just one? For that I'd recommend the Analyst role, which has read access to all calls and transcripts across the account without being scoped to a specific team. I'm pretty sure they can filter, export, and annotate but can't change any settings."},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "positive",
             "text": "Okay, so, that's exactly right. Can she also be set up to receive a weekly digest of flagged calls automatically?"},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Yes — in Notification Settings for her account you can configure a weekly summary email that includes calls flagged for quality review. You can set the threshold for what gets flagged — for example, any call with a sentiment score below 5 or where the coaching score drops a certain amount."},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "positive",
             "text": "That's perfect for our quality process. If I'm not mistaken, one other thing — is there a way to restrict which calls a supervisor sees? I've two supervisors running separate teams and they shouldn't see each other's call data."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Mhm. Yeah, so, yes — when you create their user accounts you assign them to a team group. Once a supervisor is assigned to a team, their dashboard view automatically scopes to basically that team only. They can't see other teams unless you explicitly grant them cross-team access."},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "positive",
             "text": "Wonderful. I think I've everything I need to set them up. Can I share your contact details with them in case they have questions once they're in the system?"},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Absolutely. And I'll send you a quick reference guide after this call that covers the Manager and Analyst role views — good to have on hand for new users."},
        ],
    },

    {
        "title": "Custom Branding Configuration — ProServe BPO",
        "agent": "derek.liu@callsight.ai",
        "customer_name": "Terrence Ball",
        "customer_company": "ProServe BPO",
        "customer_title": "Product Owner",
        "duration_secs": 1320,
        "source": "zoom",
        "days_ago": 16,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Okay, so, terrence, thanks for getting on a screen share. You mentioned in your email that the branding isn't sitting quite right — what specifically are you seeing?"},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball", "sentiment": "neutral",
             "text": "The logo is showing up fine but the accent color we set — our — well, my team's brand is this specific teal, hex code 00A99D — it's being overridden in places. If I'm not mistaken, the sentiment badges and a couple of the button states are still showing the default purple."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "I can see what's happening. The primary color token is applied correctly but we've a separate token for accent and interactive elements that needs to be set independently. Let me show you — in Branding Settings there are actually two color fields: Primary Brand Color, which you've set correctly, and Accent / Interactive Color, which still has the default. Can you see that on your screen?"},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball", "sentiment": "neutral",
             "text": "Oh — I see it now. It's labeled differently than I expected. Let me paste in the hex code... okay I've done it and hit save. Let me refresh. Yes! That's it, the badges are now the right color."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Good. While you're in there — do you want me to also walk you through the email template branding? When the system sends automated follow-up email drafts, those also carry your brand colors and logo, but that's a separate template config."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball", "sentiment": "positive",
             "text": "Yes actually, that's on my list. Our clients are going to see those emails so they need to look right."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Email Templates is under Settings as well. You can set your header color, upload a logo variant sized for email, and customize the footer with your contact information and unsubscribe language. The body text of the email is AI-generated per call but the wrapper is fully yours."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball", "sentiment": "positive",
             "text": "That's clean. Okay I'm going  — sorry, someone just walked in —  to run through this after our call. I think the big issue is resolved. I appreciate you jumping on screen share rather than just sending a help article."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Yeah, so, happy to. Branding details matter when your clients are seeing this. If the email template config throws you any curveballs, just shoot me a message and we'll jump back on."},
        ],
    },

    {
        "title": "Action Items Not Auto-Assigning — CoreTech Systems",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Paul Whitfield",
        "customer_company": "CoreTech Systems",
        "customer_title": "VP of Customer Success",
        "duration_secs": 2400,
        "source": "phone",
        "days_ago": 3,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "Paul, thanks for flagging this. Walk me through what you're seeing — action items are generating but not assigning to the right people?"},
            {"speaker_id": "customer", "speaker_name": "Paul Whitfield",  "sentiment": "negative",
             "text": "Correct. The action items are being extracted from the call transcripts, which is great — but they're all landing in a general pool rather than being assigned to the agent basically who was on the call. So nobody owns them and things are falling through the cracks."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "That shouldn't be happening if your agents are authenticated users in the system. When a call is uploaded or recorded, the agent's identity should flow through and auto-assign. I'm pretty sure are your agents logging calls through the integration or uploading manually?"},
            {"speaker_id": "customer", "speaker_name": "Paul Whitfield",  "sentiment": "neutral",
             "text": "Mix of both. Some agents use the Zoom integration, some are uploading MP3 files manually."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "That's likely the source of the issue for the manual uploads. When an agent uploads a file manually, the system doesn't automatically know who was on that call unless the uploaded_by field maps correctly to their user account. From what I remember, are they uploading while logged in under their own accounts, or under a shared admin account?"},
            {"speaker_id": "customer", "speaker_name": "Paul Whitfield",  "sentiment": "neutral",
             "text": "I think some of them are using a shared inbox we set up. That would explain it — everything from that shared account would have no individual assignment."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "Right, so, that's exactly it. The action item assignment uses the uploader's user identity to determine who to assign to. With a shared account, there's no individual to assign to. The fix is to make sure each agent uploads under their own account. For the historical items that landed in the pool, you can bulk-reassign them from the Action Items view using the filter and assign workflow."},
            {"speaker_id": "customer", "speaker_name": "Paul Whitfield",  "sentiment": "neutral",
             "text": "Okay, that makes sense. Can we also set it up so that even for manual uploads, a manager can specify which agent to assign to before the analysis runs?"},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "That's not currently a feature — right now assignment is automatic based on the authenticated uploader. But that's a very reasonable enhancement request. I want to log that formally because you're probably not the only team doing mixed upload workflows. Would you be okay with me raising it as a um product request with your name on it?"},
            {"speaker_id": "customer", "speaker_name": "Paul Whitfield",  "sentiment": "positive",
             "text": "Please do. And in the meantime I'll get everyone onto individual logins. It should have been set up that way from the start."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "I'll send you a quick guide on individual account setup and the bulk reassign workflow. And I've logged the feature request — it'll go to product this week."},
        ],
    },

    {
        "title": "Overage Billing Question — Horizon Telecom",
        "agent": "derek.liu@callsight.ai",
        "customer_name": "Marcus Quinn",
        "customer_company": "Horizon Telecom",
        "customer_title": "Finance Director",
        "duration_secs": 960,
        "source": "phone",
        "days_ago": 19,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "CallSight support, Derek speaking."},
            {"speaker_id": "customer", "speaker_name": "Marcus Quinn", "sentiment": "neutral",
             "text": "Hi Derek, Marcus Quinn from Horizon Telecom. I'm looking at our invoice for  — hold on one sec — okay, I'm back —  last month and there's an overage charge I wasn't expecting. I need to understand what triggered it... does that make sense?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "Of course. Let me pull up your account usage for last month. Can you give me a moment? Okay — I can see the overage. It looks like your call volume was about 23 percent above your contracted monthly limit. The spike appears to have happened in the last week of the month. Do you recall anything unusual operationally during that period?"},
            {"speaker_id": "customer", "speaker_name": "Marcus Quinn", "sentiment": "neutral",
             "text": "We ran a big promotional campaign that drove a lot of inbound calls. I guess we didn't think about how that would hit our CallSight usage as well."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "Makes sense. That explains it perfectly. The overage rate that was applied is in your contract as our standard rate, but I wanna make sure that's visible to you going forward. I think we can set up a usage alert so you're notified when you reach, say, 80 percent of your monthly limit. That would give you enough time to contact us about a temporary increase before the overage kicks in."},
            {"speaker_id": "customer", "speaker_name": "Marcus Quinn", "sentiment": "positive",
             "text": "Yes, that would be really helpful. Can you set that up for us?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "Sure, sure. I can enable it from our side right now at the 80 percent threshold. I'll also add a 90 percent alert as a second warning. You'll get an email notification to the billing contact on file — is that you, or should I add someone else?"},
            {"speaker_id": "customer", "speaker_name": "Marcus Quinn", "sentiment": "positive",
             "text": "Add our operations manager, Rosa Ingram, as well. She'd need to know before it becomes a finance problem."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "Done. Both alerts are set with you and Rosa as recipients. For next month — if you have another campaign planned, it's worth reaching out in advance and we can discuss a temporary volume increase to avoid the overage rate entirely."},
        ],
    },

    {
        "title": "Adding New Users to Account — BrightPath Financial",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Sandra Yip",
        "customer_company": "BrightPath Financial",
        "customer_title": "IT Administrator",
        "duration_secs": 720,
        "source": "phone",
        "days_ago": 22,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Hi, you've reached Aisha at CallSight. How can I help you?"},
            {"speaker_id": "customer", "speaker_name": "Sandra Yip",     "sentiment": "neutral",
             "text": "Um, hi Aisha, Sandra from BrightPath. We're onboarding a new batch of financial advisors next week and I need to add 14 new user accounts. I wanna make sure I'm doing it in bulk correctly because last time I added them one by one and it took forever."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Good news — there is a bulk import option. Under User Management there's an Import Users button that accepts a CSV. The template has columns for name, email, role, and team assignment. If you want I can send you the template file right now while we're on the phone."},
            {"speaker_id": "customer", "speaker_name": "Sandra Yip",     "sentiment": "positive",
             "text": "Yes please, that would be great. My email is sandra.yip at brightpathfinancial.com."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Sent. The template is pretty self-explanatory but the one thing to watch is the role column — it needs to match exactly: Member, Manager, or Analyst. Case-sensitive. And for team assignment, use the team ID not the team name, which you can find in Team Settings."},
            {"speaker_id": "customer", "speaker_name": "Sandra Yip",     "sentiment": "neutral",
             "text": "Team ID, not name — good to know, I would have gotten that wrong. Once I upload the CSV, do they get invite emails automatically?"},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Yes — as soon as the import runs successfully, each new user gets a welcome email with a link to set their password. The links expire after 72 hours, so time the import for a few days before they actually start so they have time to set up."},
            {"speaker_id": "customer", "speaker_name": "Sandra Yip",     "sentiment": "positive",
             "text": "Perfect. I'll do the import Thursday before their Monday start. Thanks, this is much simpler than I thought."},
        ],
    },

    {
        "title": "Missing Transcript Investigation — MediConnect Solutions",
        "agent": "derek.liu@callsight.ai",
        "customer_name": "James Orwell",
        "customer_company": "MediConnect Solutions",
        "customer_title": "Quality Assurance Lead",
        "duration_secs": 1680,
        "source": "phone",
        "days_ago": 27,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "CallSight support, Derek here."},
            {"speaker_id": "customer", "speaker_name": "James Orwell", "sentiment": "neutral",
             "text": "Well, derek, I've a specific call from last Tuesday I mean that I need the transcript for, and it's showing in our system as complete but when I click into it there's no transcript available. Just a spinning loader that never resolves."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "Let me look into that for you. Can you give me the call ID or the agent name and approximate time so I can locate it in our logs?"},
            {"speaker_id": "customer", "speaker_name": "James Orwell", "sentiment": "neutral",
             "text": "Agent is Claire Nguyen, Tuesday around 2 PM. Call with a patient services inquiry."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "Um, i've located it. I can see the call record and it does show status complete, but looking at the backend logs the transcription job actually returned an error — looks like the audio file had a codec encoding issue that caused the transcription engine to fail silently. The status should have been set to error rather than complete. That's a bug on our end... right?"},
            {"speaker_id": "customer", "speaker_name": "James Orwell", "sentiment": "negative",
             "text": "So the transcript just doesn't exist? We need that call for a compliance audit. This is a problem."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "I understand the urgency. The original audio file is still stored — we retain audio regardless of transcription status. I can manually trigger a re-transcription job against the stored audio right now. It'll run through our fallback transcription engine which handles more codec variations. It should complete within about 10 minutes."},
            {"speaker_id": "customer", "speaker_name": "James Orwell", "sentiment": "neutral",
             "text": "Please do that. And I need you to tell me — is this likely to happen with other calls as well?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "So, i'm running a check right now on your account for any other calls with the same codec pattern. Give me a moment. Okay — I found two other calls from the same source with the same issue. I'm queuing all three for re-transcription now. The root cause is that the recording system Claire's team uses sends audio in a format our primary engine handles inconsistently. I'll log a ticket to add that format to our normalization pipeline so it doesn't happen again."},
            {"speaker_id": "customer", "speaker_name": "James Orwell", "sentiment": "neutral",
             "text": "I appreciate you being thorough about it. Please email me when all three transcripts are ready."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",    "sentiment": "positive",
             "text": "Will do. You'll have an email confirmation when each one completes. And I'll follow up directly once the codec fix is in the pipeline."},
        ],
    },

    {
        "title": "Slack Integration Setup — ProServe BPO",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Terrence Ball",
        "customer_company": "ProServe BPO",
        "customer_title": "Product Owner",
        "duration_secs": 1560,
        "source": "zoom",
        "days_ago": 31,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Terrence, good to hear from you again. You mentioned wanting to set up Slack notifications — are you looking for real-time alerts or a digest?"},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Primarily real-time alerts when a call gets flagged — sentiment drops below a threshold, or if a high-priority action item is generated. Our team supervisors are in Slack all day and they want to be notified without having to check the dashboard constantly."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Yeah. That's a great use case and it's fully supported. To set it up you go to Integrations, Slack, and connect your workspace. Once that's done you can configure individual alert rules — each rule has a trigger condition and a destination channel. So you can send low-sentiment alerts to a quality-review channel and high-priority action items to a different channel for supervisors."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Can we send different alerts to different Slack channels? Like, quality flags go to the QA channel and action items go to the supervisors channel?"},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Ah, I see. Exactly — each alert rule has its own channel destination. You can create as many rules as you need with different conditions and different channels. You can even filter by team, so supervisors only get alerts for their own team's calls."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Okay, I'm in Integrations now. I can see the Slack option. Clicking connect... it's asking for workspace authorization. One second. Done. I'm in... so yeah."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Perfect. Now click Add Alert Rule. Give it a name — something like 'Low Sentiment Flag.' For the trigger, select Sentiment Score, set the condition to Less Than, and enter your threshold. What number were you thinking?"},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Our benchmark is 5 out of 10. Anything below that warrants a look."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Set it to 5. Then under Destination, choose your QA channel. Save the rule. Now create a second one for high-priority action items — trigger is Action Item Priority equals High, channel is your supervisors channel. That should cover what you described."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Both rules are saved. I'm pretty sure this is gonna make a real difference for our supervisors. They've been asking for this for weeks."},
        ],
    },

    {
        "title": "Coaching Score Explanation — CoreTech Systems",
        "agent": "derek.liu@callsight.ai",
        "customer_name": "Angela Merritt",
        "customer_company": "CoreTech Systems",
        "customer_title": "Team Lead",
        "duration_secs": 1200,
        "source": "phone",
        "days_ago": 10,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Well, hi Angela, Derek here. What's on your mind today?"},
            {"speaker_id": "customer", "speaker_name": "Angela Merritt","sentiment": "neutral",
             "text": "Hi Derek. I have a team member who is upset about his coaching score. He thinks it's unfair and I wanna understand exactly how it's calculated before I have that conversation with him. [laughs] "},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Totally reasonable. The coaching score is a composite of five behavioral signals that the AI extracts from the transcript. Do you want me to walk through each one?"},
            {"speaker_id": "customer", "speaker_name": "Angela Merritt","sentiment": "positive",
             "text": "Please."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Well, the five components are: talk-to-listen ratio, which measures how much of the call the agent is speaking versus the customer; question frequency, which tracks how often they ask open-ended questions; next step confirmation, whether a clear next step was stated and confirmed before the call ended; filler word rate, which captures um, uh, like, you know; and objection acknowledgment, whether the agent acknowledged the customer's concerns before responding. Each is scored zero to twenty and they add up to 100."},
            {"speaker_id": "customer", "speaker_name": "Angela Merritt","sentiment": "neutral",
             "text": "Which component is dragging his score down? Can I see the breakdown for a specific call?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Yes — in the call detail view, the coaching section shows the breakdown for that specific call. If his overall score is, say, 67, you can see that he might have a 19 on talk-to-listen ratio but a 9 on next step confirmation. That tells you it's a specific behavior to coach, not a general performance problem."},
            {"speaker_id": "customer", "speaker_name": "Angela Merritt","sentiment": "positive",
             "text": "That's actually really useful for the conversation. He'll respond better to specific, factual feedback than a general score. I'm pretty sure can I pull up three or four of his recent calls together in one view?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Okay, so, in the Team Performance view you can filter to his name and set a date range, and you'll see his average score per component over that period. That gives you a trend rather than a single call which is much more defensible as a coaching conversation."},
            {"speaker_id": "customer", "speaker_name": "Angela Merritt","sentiment": "positive",
             "text": "Perfect. This is exactly what I needed. I feel prepared for the conversation now."},
        ],
    },

    {
        "title": "CSV Data Export Request — Horizon Telecom",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Rosa Ingram",
        "customer_company": "Horizon Telecom",
        "customer_title": "Operations Analyst",
        "duration_secs": 600,
        "source": "phone",
        "days_ago": 33,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Aisha here — what can I help you with, Rosa?"},
            {"speaker_id": "customer", "speaker_name": "Rosa Ingram",    "sentiment": "positive",
             "text": "Hi Aisha! We spoke before about the sentiment export. I have a new request — my director wants a full export of all call transcripts for Q1, not just the metrics. Is that possible?"},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "So, absolutely. In the Data Exports section — the same place you found the sentiment export — there's a Transcript Export option. You can choose Full Text or Segments, and set the date range to Q1. Full Text gives you the entire transcript as one block per call; Segments gives you each speaker turn as a separate row with timestamps and sentiment. Which would be more useful for your director?"},
            {"speaker_id": "customer", "speaker_name": "Rosa Ingram",    "sentiment": "positive",
             "text": "Probably the full text for readability. The segments might be too granular."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Well, full text it is. One thing to note — Q1 is a large dataset. The export runs asynchronously and you'll get an email with a download link when it's ready, which could take 10 to 15 minutes depending on volume."},
            {"speaker_id": "customer", "speaker_name": "Rosa Ingram",    "sentiment": "positive",
             "text": "That's fine. I'll kick it off now and grab I mean the download later. Thanks for always being so quick to help, Aisha... right?"},
        ],
    },

    {
        "title": "Admin Access for New Manager — BrightPath Financial",
        "agent": "derek.liu@callsight.ai",
        "customer_name": "Owen Cartwright",
        "customer_company": "BrightPath Financial",
        "customer_title": "Sales Operations Manager",
        "duration_secs": 780,
        "source": "phone",
        "days_ago": 40,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",       "sentiment": "positive",
             "text": "Derek here, how can I help?"},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "neutral",
             "text": "Okay, so, owen at BrightPath. We have a new regional sales manager starting Monday. She needs admin access to manage users and view all team call data. Currently I'm the only admin and I'd like to add her."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",       "sentiment": "positive",
             "text": "Easy to do. You can add her as an Admin from User Management under your account settings. Admins can manage users, configure integrations, access all team data, and view billing. Is that the level of access she should have, or should we limit billing visibility?"},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "neutral",
             "text": "Let's keep billing limited to me only. Is there an admin-without-billing role?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",       "sentiment": "positive",
             "text": "There's a Manager role that has full user management and all-team data access but no billing access. That's probably the right fit. It's one step below Admin. I'd recommend that over creating a custom Admin without billing, which isn't a standard configuration."},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "positive",
             "text": "Manager role sounds right. Her email is diane.cole at brightpathfinancial.com. Can you add her or does it have to come from my end?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",       "sentiment": "positive",
             "text": "You'll need to add her from your account since it requires your admin authorization — I can't provision users on your behalf for security reasons. But it's just two clicks: User Management, Invite User, enter her email, set role to Manager. She'll get a welcome email with setup instructions immediately."},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "positive",
             "text": "Yeah, so, done already. She'll be ready before Monday. Thanks for the quick answer."},
        ],
    },

    {
        "title": "AI Summary Feedback — MediConnect Solutions",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Claire Nguyen",
        "customer_company": "MediConnect Solutions",
        "customer_title": "Patient Services Manager",
        "duration_secs": 1800,
        "source": "zoom",
        "days_ago": 44,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Claire, always good to connect. You mentioned the AI summaries feel generic — can you give me a specific example so I understand exactly what you mean?"},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "neutral",
             "text": "Sure. Our calls often involve very specific clinical terminology and patient service protocols. The summaries come back very general — things like 'agent addressed customer concern and provided resolution.' That's technically correct but tells our quality team nothing about what actually happened on the call."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "That's really useful feedback. The default summary prompt is intentionally broad to work across industries, but we can tune it specifically for your context. Can you tell me what a good summary would look like for your team? What are the three or four things it should always capture?"},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "positive",
             "text": "Alright, so, for us — the nature of the patient's inquiry, the specific department or service they were directed to, whether they were satisfied or needed escalation, and any follow-up that was committed to. Those four things."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Perfect. We can configure a custom summary template that instructs the AI to always extract those four elements. I can set that up for your account this week. The summaries will still be generated by AI but the prompt will be tuned to your workflow, so the output will be much more specific to what your team actually needs to see."},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "positive",
             "text": "That would be a significant improvement. Right now our supervisors are reading full transcripts because the summaries aren't good enough, which defeats the whole time-saving purpose."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "I understand — the summary is supposed to replace the transcript read, not send people to the transcript anyway. I'm gonna set this as a priority configuration task. I'll have a revised prompt template to you by end of this week and we can review the output on a sample set of your recent calls together before rolling it out fully."},
            {"speaker_id": "customer", "speaker_name": "Claire Nguyen",  "sentiment": "positive",
             "text": "That process sounds exactly right. I appreciate you taking this seriously rather than just saying the product works as designed."},
        ],
    },

    {
        "title": "Quarterly Business Review — ProServe BPO",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Terrence Ball",
        "customer_company": "ProServe BPO",
        "customer_title": "Product Owner",
        "duration_secs": 3600,
        "source": "zoom",
        "days_ago": 48,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Well, terrence, thanks for making time for our quarterly review. I want to start by sharing what the data is showing for your account over the last 90 days, and then I want to hear from you on what's working and what isn't."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Looking forward to it. The team has been using the platform pretty heavily so there should be a lot to talk about."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "The headline numbers: you processed 4,200 calls last quarter, up from 2,800 in the prior quarter — 50 percent growth in usage. Average sentiment across your call volume was 7.2 out of 10, which is in the healthy range. From what I remember, action item completion rate was 68 percent, meaning about a third of extracted action items are going undone. That's the one area I want to dig into with you."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "neutral",
             "text": "Honestly, the action item completion rate is something we've been discussing internally. I think part of the issue is that action items are generating but some agents aren't checking the platform to see them. They're used to their old workflow."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "That's a very common adoption challenge. The Slack integration you set up last month should help — when a high-priority action item generates, the supervisor gets a Slack ping, which is harder to ignore than a dashboard notification. Are supervisors actually using that?"},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Yes, the Slack alerts are being used actively. It's more the medium-priority items that fall through. Those don't trigger a Slack alert so agents don't see them unless they log in."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "One option is to lower the Slack alert threshold to include medium-priority items, or alternatively we can set up a daily digest that summarizes all open action items for each agent via email every morning. That way nothing requires proactive log-in — it comes to them."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "The daily digest idea is better than more Slack pings. Can we set that up today?"},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Yes — I'll configure it while we're on the call. Set to go out at 8 AM daily, each agent receives only their own open items. Done. On the coaching side — your team's average coaching score improved from 71 to 78 over the quarter. That's a meaningful 10 percent improvement. Something is working."},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Our supervisors have been actually using the coaching notes in their 1-on-1s. It's changed the culture of those conversations. People used to get defensive because feedback was vague. Now there's a transcript to reference and it's much more constructive."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "Right, so, that's exactly the outcome this should produce. Coaching moves from impressions to evidence. Is there anything on your roadmap for next quarter that I should know about from a platform perspective?"},
            {"speaker_id": "customer", "speaker_name": "Terrence Ball",  "sentiment": "positive",
             "text": "Look, we're potentially adding two new client accounts which would increase our call volume significantly. I want to make sure capacity isn't an issue."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson", "sentiment": "positive",
             "text": "No capacity concerns on our end — the platform scales automatically. But let me know when those accounts are confirmed so I can flag to our team and make sure your onboarding for the new client configurations goes smoothly."},
        ],
    },

    {
        "title": "Feature Request — Advanced Filtering — CoreTech Systems",
        "agent": "derek.liu@callsight.ai",
        "customer_name": "Paul Whitfield",
        "customer_company": "CoreTech Systems",
        "customer_title": "VP of Customer Success",
        "duration_secs": 1140,
        "source": "phone",
        "days_ago": 55,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",      "sentiment": "positive",
             "text": "Right, so, paul, good to hear from you. What's on your mind?"},
            {"speaker_id": "customer", "speaker_name": "Paul Whitfield", "sentiment": "neutral",
             "text": "I have a feature request rather than a support issue. I want to be able to filter calls by the type of customer — we tag our customers by segment in Salesforce, like Enterprise, Mid-Market, SMB. I want to see call quality metrics broken down by segment, not just by agent or team."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",      "sentiment": "positive",
             "text": "Ah, I see. That's a great use case. Right now the filtering is by agent, team, date range, and sentiment threshold. Segment-level filtering would require that segment data to flow in from Salesforce and be stored against the call record as metadata. It's technically feasible but not currently a standard feature."},
            {"speaker_id": "customer", "speaker_name": "Paul Whitfield", "sentiment": "neutral",
             "text": "Is it something that could be done through the custom metadata fields? I noticed you can attach metadata to calls via the API."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",      "sentiment": "positive",
             "text": "Right, so, that's actually a clever workaround. If you push the customer segment from Salesforce into the call metadata when the Salesforce integration fires, it would be stored on the call record. We don't currently surface custom metadata as a filter dimension in the dashboard, but that's a smaller product ask than building native Salesforce segment sync. Let me log both requests — the workaround path and the proper feature — so product can evaluate."},
            {"speaker_id": "customer", "speaker_name": "Paul Whitfield", "sentiment": "neutral",
             "text": "Alright, so, i'd actually be willing to be a beta user for the metadata filtering feature if it moves up the roadmap. Having enterprise vs. SMB call quality comparison would be really valuable for us... you know what I mean?"},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",      "sentiment": "positive",
             "text": "Okay, so, i'll note you as an interested beta user — that carries weight when product prioritizes. I'll make sure this ticket gets to your account's product contact with your willingness to test called out explicitly."},
        ],
    },

    {
        "title": "Password Reset & Account Lockout",
        "agent": "derek.liu@callsight.ai",
        "customer_name": "Beth Sorenson",
        "customer_company": "Horizon Telecom",
        "customer_title": "Customer Success Specialist",
        "duration_secs": 420,
        "source": "phone",
        "days_ago": 60,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "CallSight support, Derek here."},
            {"speaker_id": "customer", "speaker_name": "Beth Sorenson", "sentiment": "negative",
             "text": "Hi, I'm locked out of my account. As far as I know, i tried to reset my password three times and now it's saying my account is locked. I've a team meeting in 20 minutes where I need to pull up call data."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Sure, sure. I can fix that right now. What's your email address?"},
            {"speaker_id": "customer", "speaker_name": "Beth Sorenson", "sentiment": "neutral",
             "text": "beth.sorenson at horizontelecom.com."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Got it. I'm unlocking the account now and sending a fresh password reset link directly to that email. It'll arrive in about — well, roughly 30 seconds. The lockout triggers after five failed attempts as a security measure — you should be able to get in right after resetting."},
            {"speaker_id": "customer", "speaker_name": "Beth Sorenson", "sentiment": "positive",
             "text": "I see the email. Resetting now... I'm in. Thank you, that was really fast."},
            {"speaker_id": "agent",    "speaker_name": "Derek Liu",     "sentiment": "positive",
             "text": "Glad we got you sorted. Good luck with the meeting."},
        ],
    },

    {
        "title": "Frustrated Client — Multiple Ongoing Issues — BrightPath Financial",
        "agent": "aisha.thompson@callsight.ai",
        "customer_name": "Owen Cartwright",
        "customer_company": "BrightPath Financial",
        "customer_title": "Sales Operations Manager",
        "duration_secs": 2700,
        "source": "phone",
        "days_ago": 62,
        "segments": [
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "Aisha Thompson. Thanks for calling, Owen. What can I help you with today?"},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "negative",
             "text": "Okay, so, aisha, I need to be candid with you. We've had three support issues in the past six weeks and while they've all been resolved eventually, the experience has not been great. The Salesforce sync broke and took two days to diagnose. Before that we had a transcript that disappeared. And now I'm hearing from my reps that the app is slow in the afternoons. I'm starting to wonder if this was the right investment."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "neutral",
             "text": "Owen, I really appreciate you saying this directly rather than letting it fester. You're right that three issues in six weeks is too many, and I'm not gonna defend that. What I wanna do is understand the pattern, own it, and put a plan in front of you."},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "negative",
             "text": "The Salesforce sync issue should have had a better error message — your own support engineer said that. The transcript issue was a codec problem your system should have caught. And now performance issues that no one has proactively told me about. It feels reactive."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "neutral",
             "text": "All fair points. On the performance issue — this is the first I'm hearing of it and I want to address that immediately. Can you tell me more specifically? Which part of the app is slow, at what times?"},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "negative",
             "text": "Loading the transcript view for calls from that day. Reps said between about 2 and 4 PM it can take 15 to 20 seconds to load a transcript. If I'm not mistaken, that's unusable."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "15 to 20 seconds isn't acceptable and I'm treating this as urgent. I'm flagging this to our engineering team right now while we're on the call. That time window — 2 to 4 PM — suggests like a concurrency issue during peak usage hours. I want to get our technical team eyes on this today."},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "neutral",
             "text": "I appreciate that. But I also need to know what you're going to do about the pattern. I can't keep calling support every two weeks."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "I hear you. Here's what I'm going to do. First, I'm personally going to be your single point of contact going forward — anything that comes up, you email me directly and I own the resolution. Second, I'm going to set up a monthly check-in with you — 20 minutes — so we're talking proactively, not just when something breaks. Third, I'm going to pull together a written incident summary of the three issues with root cause and what's been done to prevent recurrence. You'll have that by end of this week."},
            {"speaker_id": "customer", "speaker_name": "Owen Cartwright", "sentiment": "neutral",
             "text": "That's a reasonable response. I wanna see follow-through on it, but it's a reasonable response."},
            {"speaker_id": "agent",    "speaker_name": "Aisha Thompson",  "sentiment": "positive",
             "text": "I understand. I'm putting  — sorry, someone just walked in —  the incident summary in my calendar for Thursday. And you'll hear from me today on the performance issue once I have an update from engineering. I genuinely wanna get this right for you, Owen."},
        ],
    },

]

# ── Insert functions ───────────────────────────────────────────────────────────

def fetch_tenant_and_users(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tenants WHERE slug = 'callsight-demo'")
        tenant_id = cur.fetchone()[0]
        cur.execute("SELECT email, id FROM users WHERE tenant_id = %s", (tenant_id,))
        user_map = {row[0]: row[1] for row in cur.fetchall()}
    return tenant_id, user_map

def insert_cs_calls(conn, tenant_id, user_map):
    total_calls = 0
    total_segs  = 0

    with conn.cursor() as cur:
        for c in CS_CALLS:
            call_id       = new_id()
            transcript_id = new_id()
            agent_uid     = user_map[c["agent"]]
            call_date     = days_ago(c["days_ago"])
            participants  = [
                {"name": c["customer_name"], "company": c["customer_company"],
                 "title": c["customer_title"], "role": "customer"},
                {"name": next(a["full_name"] for a in [
                    {"email": "aisha.thompson@callsight.ai", "full_name": "Aisha Thompson"},
                    {"email": "derek.liu@callsight.ai",      "full_name": "Derek Liu"},
                ] if a["email"] == c["agent"]), "role": "agent"},
            ]

            cur.execute("""
                INSERT INTO calls
                  (id, tenant_id, uploaded_by, title, call_type, participants,
                   duration_secs, source, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                call_id, tenant_id, agent_uid,
                c["title"], "support",
                json.dumps(participants),
                c["duration_secs"], c["source"], "complete",
                call_date, call_date,
            ))

            segments   = c["segments"]
            full_text  = build_full_text(segments)
            word_count = len(full_text.split())

            cur.execute("""
                INSERT INTO transcripts
                  (id, call_id, tenant_id, full_text, word_count, engine)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (transcript_id, call_id, tenant_id, full_text, word_count, "whisper"))

            cursor_ms = 0
            for i, seg in enumerate(segments):
                seg_id = new_id()
                end_ms = ms_from_words(seg["text"], cursor_ms)
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
    print(f"✓ {total_calls} customer service calls inserted ({total_segs} transcript segments)")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\nConnecting to Neon (Flex)...")
    conn = psycopg2.connect(DB_URL)
    print("✓ Connected\n")

    tenant_id, user_map = fetch_tenant_and_users(conn)
    print(f"✓ Tenant found: {tenant_id}")
    print(f"✓ Users loaded: {list(user_map.keys())}\n")

    insert_cs_calls(conn, tenant_id, user_map)

    with conn.cursor() as cur:
        cur.execute("SELECT call_type, COUNT(*) FROM calls GROUP BY call_type ORDER BY call_type")
        for row in cur.fetchall():
            print(f"  {row[0]}: {row[1]} calls")
        cur.execute("SELECT COUNT(*) FROM transcript_segments")
        print(f"  total segments: {cur.fetchone()[0]}")

    conn.close()
    print("\n✓ Done.\n")

if __name__ == "__main__":
    main()
