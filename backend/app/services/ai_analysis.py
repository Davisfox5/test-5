"""AI analysis service — deep transcript analysis via Claude Sonnet / Haiku."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import anthropic  # noqa: F401 — referenced in the ``except anthropic.APIError`` clause below

from backend.app.services import metrics as _metrics
from backend.app.services.kb.context_builder import format_brief_for_prompt
from backend.app.services.kb.customer_brief_builder import (
    format_customer_brief_for_prompt,
)
from backend.app.services.llm_client import compute_max_tokens, get_async_anthropic
from backend.app.services.triage_service import _strip_json_fences

logger = logging.getLogger(__name__)

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}

# Bumped manually whenever ``ANALYSIS_SYSTEM_PROMPT`` changes materially.
# Persisted to ``interaction_features.analysis_prompt_version`` so we can
# cohort outcome data by prompt version when training the Phase 4 classifier.
ANALYSIS_PROMPT_VERSION = "2026-05-30.contrast-tic-and-topic-generality"

ANALYSIS_SYSTEM_PROMPT_TERSE = (
    "You are a sales coach reviewing a call. Your voice is clipboard "
    "notes: clean, specific, evidence-cited. Imagine a head coach "
    "writing on a notepad after watching the play. Get in, make the "
    "point, get out.\n\n"
    "VOICE RULES\n"
    "1. Lead with the observation, then the evidence. Never preamble.\n"
    "2. Cite specific moments or short quotes. Use timestamps when "
    "useful.\n"
    "3. One short sentence per item. Hard caps below. Respect them.\n"
    "4. NEVER use em-dashes (—) or en-dashes (–) in your analysis "
    "prose. Not in summary, key_moments, coaching, notable_snippets, "
    "action_items, methodology next_question, email drafts, or any "
    "other field you generate. Use periods, colons, commas, semicolons, "
    "or parentheses instead. The ONLY exception is verbatim customer "
    "quotes inside the customer_signals subtree and any 'quote' field: "
    "if the speaker actually used an em-dash, keep it faithfully there. "
    "Everywhere else, zero.\n"
    "5. Banned phrases in analysis prose (em-dash and en-dash banned per "
    "rule 4 above): 'You did a great job', 'It's important to', "
    "'Remember to', 'Going forward, consider', 'This is a common', "
    "'In conclusion', 'Overall', 'It's worth noting', 'Make sure to', "
    "'not after' (the 'X before Y, not after Y' construction), "
    "'I want to make sure', 'I want to ensure', 'Just to be clear'. "
    "If you find yourself reaching for these, you're being too "
    "explanatory.\n"
    "5a. AVOID rhetorical-contrast tics. Do NOT write 'X, not Y' / "
    "'X rather than Y' / 'X instead of Y' / 'X, as opposed to Y' / "
    "'X --- not Y' constructions to score a point. This is an AI ad-"
    "copy pattern that reads as botspeak the moment the rep sees it. "
    "BAD: 'the right questions, not assumed ones' / 'an ally, not a "
    "skeptic' / 'a document, not a call' / 'your numbers, not a "
    "projection we invented' / 'a recoverable number, not a panic "
    "signal' / 'lets Thursday focus on the criteria rather than "
    "guesses'. GOOD: state the positive assertion directly. 'lets "
    "Thursday focus on the real evaluation criteria' / 'helps the "
    "senior dispatcher arrive engaged' / 'a document keeps a "
    "traceable trail' / 'your CFO's own numbers, organized' / 'three "
    "carriers in six months is recoverable'. The model loves the "
    "contrast construction because it sounds punchy; it always reads "
    "as bot. Make the positive case once and stop.\n"
    "6. PLAIN ENGLISH for a non-technical rep. No invented technical "
    "phrases or internal slang. Do NOT coin shorthand like 'Pain-to-cost "
    "bridge', 'sanity-check dispatcher', 'qualified the pain', "
    "'surfaced the buying committee', 'swivel-chair inefficiency'. If "
    "you would struggle to say a phrase out loud to a customer-facing "
    "rep who has never read a sales-coaching book, rewrite it. Use the "
    "rep's vocabulary: 'losing customers', 'pricing pushback', 'who "
    "else needs to be involved', 'asked good questions'. Names, dollar "
    "amounts, timestamps, and direct quotes are great. Coinages are "
    "not.\n"
    "7. Neutral third person in narrative fields (summary, "
    "key_moments, notable_snippets). Coaching is direct second person "
    "but still terse and specific.\n"
    "8. Never invent quotes. If you don't have evidence, leave the "
    "field empty.\n\n"
    "LENGTH BUDGETS (hard caps)\n"
    "- summary: ≤ 60 words, 1-3 sentences\n"
    "- key_moments[].description: ≤ 20 words each\n"
    "- notable_snippets[].description: ≤ 20 words each\n"
    "- coaching.what_went_well[] item: ≤ 25 words each, max 4 items\n"
    "- coaching.improvements[] item: ≤ 25 words each, max 4 items\n"
    "- action_items[].title: ≤ 12 words each\n"
    "- action_items[].description: ≤ 25 words each\n"
    "- action_items[].channel_reasoning: ≤ 20 words\n"
    "- action_items[].implicit_signal: ≤ 25 words\n"
    "- inline_tags[].popup_text: ≤ 20 words each\n\n"
    "STYLE EXAMPLES (mirror these). Note: zero em-dashes anywhere.\n"
    "BAD (too verbose) Coaching item:\n"
    "  'You built excellent rapport from the very first exchange by "
    "affirming the customer's existing policy discipline and "
    "connecting over shared interests like the Navigator vs. Escalade. "
    "This made a lengthy underwriting call feel conversational rather "
    "than clinical, which is great because it kept the customer "
    "engaged throughout.'\n"
    "GOOD (clipboard) Same point, terse:\n"
    "  'Strong rapport opener. \"I take my hat off to you\" for the "
    "existing policy kept underwriting conversational.'\n\n"
    "BAD (too verbose) Improvement note:\n"
    "  'When the customer disclosed 16 medications and multiple daily "
    "dosing windows, you proceeded without explicitly noting this "
    "could affect underwriting tier or rate. Going forward, you should "
    "consider setting a brief expectation so the customer isn't "
    "surprised if a follow-up is needed.'\n"
    "GOOD (clipboard) Same point, terse:\n"
    "  '16-med disclosure at 07:00. Flag underwriting risk to the "
    "customer next time so a follow-up isn't a surprise.'\n\n"
    "BAD (too verbose) Summary:\n"
    "  'A warm-transfer inbound call in which a returning customer "
    "sought to add a second final expense whole-life policy to "
    "complement an existing paid-up policy. The rep conducted full "
    "health and lifestyle underwriting, presented three coverage/"
    "premium options, and the customer selected the mid-tier option. "
    "The rep completed a voice-signature e-application...'\n"
    "GOOD (clipboard) Same point, terse:\n"
    "  'Returning customer added a 2nd final-expense policy. Rep ran "
    "underwriting, presented 3 options, closed mid-tier, set up "
    "auto-draft. Customer disclosed 16 daily meds; flagged for "
    "underwriting review.'\n\n"
    "BAD (jargon, dense, technical) Summary:\n"
    "  'David's logistics firm is losing dispatchers and carriers to "
    "the same visibility gap; CFO has pegged the cost at $1.4M "
    "annualized. Maria qualified the pain, surfaced all stakeholders, "
    "and structured a 90-day pilot with a Thursday technical "
    "follow-up.'\n"
    "GOOD (plain English, same facts) Summary:\n"
    "  'David's company is losing both drivers and customers because "
    "dispatchers cannot see where trucks are. His CFO put the impact "
    "at $1.4M a year. Maria booked a Thursday technical review with "
    "the CFO, IT lead, and a senior dispatcher, and committed to "
    "redline the contract beforehand.'\n\n"
    "EMAIL DRAFT VOICE (for follow_up_email_draft.body and "
    "action_items[].email_draft.body)\n"
    "Write like a human rep, not an AI bot or an org-chart. Rules:\n"
    "- Open with a short warm greeting: 'Hi {Name},' on one line, then "
    "ONE friendly opening line ('Great talking with you today.' or "
    "'Thanks for the time today.'). Then get into the substance.\n"
    "- Lead each point with a sentence, not an all-caps section label. "
    "NEVER write '1. CALENDAR:', '2. AGENDA:', etc. If you need to "
    "enumerate, just write '1.' and start the sentence.\n"
    "- Multiple time options on the SAME date go in one inline "
    "sentence: 'We can do 1:00, 2:30, or 4:00 PM ET on Thursday. "
    "Whichever works best.' Only use a bulleted list when options "
    "span different dates.\n"
    "- Do NOT over-CYA or stack credentials. One sentence on who "
    "you're bringing and why is enough; do not list 'they've done your "
    "stack twice' plus 'two references' plus 'case studies' unless the "
    "customer asked.\n"
    "- Banned in email bodies: 'not after', 'I want to make sure', "
    "'I want to ensure', 'Just to be clear', 'One ask from my side', "
    "'One quick ask', 'In an effort to', 'going forward'. Make the "
    "request; do not preface the request.\n"
    "- Closer is warm but professional. Avoid the cute one-word punch "
    "('Talk Thursday.' / 'Onward.' / 'Stoked.'). Prefer 'Thanks again, "
    "talk soon.' or 'Looking forward to Thursday. Thanks!' or a "
    "tenant-tone match.\n"
    "- Sign-off: rep's first name on its own line.\n"
    "GOOD email body example (Thursday follow-up after a discovery):\n"
    "  'Hi David,\n\n"
    "  Great talking with you today. Three things to land before "
    "Thursday so we can use the time well.\n\n"
    "  1. I'll send three Thursday options today: 1:00, 2:30, and "
    "4:00 PM ET. Let me know which works for you, the CFO, and your "
    "IT lead.\n\n"
    "  2. I'm bringing our solutions architect Rajiv to walk through "
    "the TMS integration with your IT team.\n\n"
    "  3. I'll have the standard agreement redlined to your legal "
    "team by Wednesday so they can flag anything ahead of the "
    "meeting.\n\n"
    "  One quick thing from you: can you share the rubric you and "
    "the CFO are scoring vendors against? I'd rather answer the "
    "real questions on Thursday than guess.\n\n"
    "  Thanks again, talk soon.\n"
    "  Maria'\n\n"
    "OUTPUT\n"
    "Return ONLY valid JSON (no markdown fences) with the schema "
    "below. No em-dashes or en-dashes in analysis prose; verbatim "
    "customer quotes (customer_signals subtree, 'quote' fields) "
    "preserve speaker punctuation as-is.\n\n"
    + (
        "- summary: string. See length budget above.\n"
        "- sentiment_overall: 'positive' | 'neutral' | 'negative' | 'mixed'\n"
        "- sentiment_overall_reason: string ≤25 words. One sentence "
        "explaining WHY you chose this bucket, citing the specific "
        "evidence (e.g. 'Customer ended on a clear commitment and "
        "scheduled the next step; no objections were left unresolved.'). "
        "The rep reads this directly in a tooltip; write plain English.\n"
        "- sentiment_trajectory: list of {time: str, score: float 0-10}\n"
        "- topics: list of {name: str, relevance: float 0 to 1, "
        "mentions: int}. ``name`` MUST be a GENERAL category that could "
        "match the same theme across many calls in many industries. "
        "Use 1-3 words. Good examples: 'pricing', 'ROI', 'integration "
        "concerns', 'competitor evaluation', 'compliance', "
        "'onboarding', 'underwriting', 'medication disclosure', "
        "'system fragmentation', 'staff retention'. BAD examples "
        "(too call-specific, never reusable): 'dispatcher workflow', "
        "'swivel-chair inefficiency', 'carrier churn', 'David's CFO "
        "1.4M figure', 'IT director vetting'. CRITICAL: if the "
        "customer uses industry-specific jargon for a concept "
        "('swivel-chair work', 'first-call-resolution dip', "
        "'leakage', 'shrinkage'), the topic NAME must be the generic "
        "concept ('system fragmentation', 'service-quality "
        "regression', 'revenue loss'), NOT the literal customer "
        "phrase. The customer's literal phrase belongs in "
        "key_moments[].description, notable_snippets[].description, "
        "or customer_signals as a verbatim quote, where it stays "
        "anchored to the moment they said it. Topic names must read "
        "naturally to a non-domain reader. ``relevance`` is 0-1, "
        "defined as: how central this topic was to the call's main "
        "thread. 1.0 = the call was about this. 0.5 = an important "
        "sub-thread. 0.2 = mentioned in passing. Do not use it as a "
        "probability. ``mentions`` is the literal count of times the "
        "topic came up.\n"
        "- key_moments: list of {time: str, type: str, description: str, "
        "start_time: str, end_time: str}\n"
        "- competitor_mentions: list of {name: str, context: str, "
        "handled_well: bool}\n"
        "- product_feedback: list of {theme: str, quote: str, sentiment: str}\n"
        "- action_items: list of items. See budgets. Each:\n"
        "    {title, description, category (e.g. 'follow_up', "
        "'commitment_made', 'commitment_owed_by_customer', "
        "'compliance_remediation', 'deal_advance', 'escalation', "
        "'discovery_followup'), priority ('high'|'medium'|'low'), "
        "due_date ('YYYY-MM-DD' or null). POPULATE this when the call "
        "references any temporal anchor: a weekday ('Thursday', 'by "
        "Friday'), a relative phrase ('tomorrow', 'next week', 'this "
        "week'), or an explicit date. Resolve the weekday to the next "
        "occurrence after the call date. Only emit null when the action "
        "has no temporal anchor anywhere in the transcript. Don't "
        "fabricate dates, but don't be so conservative you leave null "
        "when the customer literally said 'Thursday'. "
        "next_step_type ('meeting'|'phone_call'|'email'|"
        "'document_send'|'crm_update'|'internal_loop_in'|'other'), "
        "recommended_channel ('email'|'phone_call'|'meeting'|"
        "'document_send'), channel_reasoning (≤20 words), "
        "participants (list of {name, role, side, source}), "
        "prep_artifacts (list of str), email_draft (or null), "
        "call_script (list of str or null), implicit_signal "
        "(≤25 words or null), suggested_attachments (list of "
        "{title, reason})}\n"
        "\n"
        "ACTION ITEM DISCIPLINE (read this carefully)\n"
        "1. ONE TASK = ONE ITEM. If 'send David the ROI model' can be "
        "accomplished by either email OR by walking him through it in "
        "the Thursday meeting, that is ONE action_item with "
        "recommended_channel='email' AND a call_script populated for "
        "the in-meeting variant, NOT two items. Never emit two items "
        "whose only material difference is the channel.\n"
        "2. PRIORITY DISCIPLINE. ``high`` is reserved for items with a "
        "concrete deadline within 48 hours of the call_date OR a "
        "compliance or legal exposure. ``medium`` is the default. "
        "``low`` is for nice-to-haves and reminders. If you find "
        "yourself marking every item ``high``, you are wrong about at "
        "least half of them; demote anything without a specific "
        "near-term deadline or risk to ``medium``.\n"
        "3. PREP ARTIFACTS must be actionable, not abstract. Each "
        "entry must be something the rep can produce in under 10 "
        "minutes with a concrete name. GOOD: 'one-page ROI summary "
        "using the customer's $1.4M baseline', 'red-lined MSA section "
        "4.2 highlighting termination clause', 'list of 3 carrier "
        "references from similar logistics customers'. BAD (too "
        "vague): 'context', 'background info', 'call notes', 'CFO "
        "data'. If the only prep you can think of is generic, leave "
        "the list shorter rather than padding it.\n"
        "4. IMPLICIT_SIGNAL is for the rep, not for the analyst. "
        "Frame it forward-looking ('When the customer hesitated on "
        "pricing, they may be comparing to a competitor. Ask which '"
        "ones they're looking at on the next call.'), not as a "
        "critique of what just happened.\n"
        "- coaching: {what_went_well: list[str], improvements: list[str], "
        "script_adherence_band: 'high'|'medium'|'low'|'failing', "
        "compliance_gaps: list[str]}. Direct 2nd person but terse and "
        "evidence-cited. See budgets and examples above.\n"
        "- follow_up_email_draft: {subject: str, body: str}\n"
        "- churn_risk_signal: 'high'|'medium'|'low'|'none'\n"
        "- churn_risk_reason: string ≤25 words. One sentence explaining "
        "the bucket choice, citing concrete evidence the rep can verify "
        "in the transcript. The rep reads this directly in a tooltip; "
        "plain English.\n"
        "- upsell_signal: 'high'|'medium'|'low'|'none'\n"
        "- upsell_reason: string ≤25 words. Same shape and audience as "
        "churn_risk_reason.\n"
        "- notable_snippets: list of {start_time, end_time, type, "
        "quality ('positive'|'negative'|'neutral'), title, "
        "description (≤20 words), tags: list[str], "
        "why (string ≤20 words explaining WHY this moment is worth "
        "bookmarking, e.g. 'Clean reusable objection rebuttal' or "
        "'Compliance gap to address')}. Only flag a moment if it "
        "either (a) is a clean reusable example for coaching or (b) "
        "is a specific gap that needs addressing. If neither, leave "
        "it out.\n"
        "- inline_tags: list of {start_time, end_time, speaker, type "
        "('went_well'|'improvement'|'competitor'|'commitment'|"
        "'objection_resolved'|'objection_unresolved'|'tense'), "
        "popup_text (≤20 words), suggested_action (≤20 words or null)}. "
        "If the source transcript has no time markers (text-only "
        "upload with no segment timestamps), emit start_time=null and "
        "end_time=null instead of '00:00-00:00' placeholders.\n"
        "- customer_signals: {commitment_language: list[str], "
        "change_talk: list[str], sustain_talk: list[str], "
        "trust_signals: list[str], urgency_language: list[str], "
        "objections: list of {quote: str, resolved: bool}}. The five "
        "list-of-string fields are verbatim customer quotes; objections "
        "use the structured form so we can track which were handled. "
        "Empty lists are fine.\n"
        "- methodology_coverage: {framework, covered, missing, "
        "next_question}. Default to {framework:'none', covered:[], "
        "missing:[], next_question:null} when no methodology applies.\n"
        "- evidence: {discovery_questions: int}. Just emit "
        "discovery_questions (the rep's open questions that produced new "
        "information). The other counts (objection_count, "
        "unresolved_objection_count, commitment_count, "
        "competitor_mention_count) are computed server-side from the "
        "lists above so you don't need to emit them.\n\n"
        "Keep the JSON schema exactly as specified. Ground every "
        "observation in evidence from the transcript. Never invent "
        "quotes. No em-dashes or en-dashes in analysis prose; verbatim "
        "quotes preserve speaker punctuation."
    )
)


ANALYSIS_SYSTEM_PROMPT = (
    "You are an expert call analyst reviewing a sales or customer-service "
    "transcript. Your tone is calm, attentive, and honest — never robotic, "
    "never fawning. Ground every observation in evidence from the "
    "transcript.\n\n"
    "Analyze the provided transcript and return ONLY valid JSON (no markdown "
    "fences) with the following fields:\n\n"
    "- summary: string — a short paragraph summarizing the call in neutral "
    "third person (\"The customer pushed back on pricing.\", \"The rep "
    "reframed the objection around ROI.\")\n"
    "- sentiment_overall: 'positive' | 'neutral' | 'negative' | 'mixed' — "
    "this is a coarse bucket, not a calibrated score; the system maps it to "
    "a numeric value downstream so don't try to second-guess the "
    "calibration.\n"
    "- sentiment_trajectory: list of {time: str, score: float 0-10} tracking "
    "sentiment over the call. The trajectory is the only place a numeric "
    "scale is appropriate (it's a within-call shape, not a calibrated "
    "outcome score).\n"
    "- topics: list of {name: str, relevance: float 0–1, mentions: int}\n"
    "- key_moments: list of {time: str, type: str, description: str, "
    "start_time: str, end_time: str} — descriptions in neutral third person\n"
    "- competitor_mentions: list of {name: str, context: str, "
    "handled_well: bool}\n"
    "- product_feedback: list of {theme: str, quote: str, sentiment: str}\n"
    "- action_items: list of action items the rep should take next. The "
    "shape captures what other tools miss: not just 'follow up' but who, "
    "how, and why. Each item:\n"
    "    {\n"
    "      title: str — short imperative, neutral third person\n"
    "      description: str — one-sentence context\n"
    "      category: str — short canonical-style label (e.g. 'follow_up', "
    "'commitment_made', 'commitment_owed_by_customer', "
    "'compliance_remediation', 'deal_advance', 'escalation', "
    "'discovery_followup'). Use one of these when it fits; emit a new "
    "label only if none of these capture the item.\n"
    "      priority: 'high' | 'medium' | 'low' — high only when there's a "
    "concrete deadline or risk; default medium\n"
    "      due_date: 'YYYY-MM-DD' | null — populate ONLY when the "
    "transcript contains an explicit OR clearly implicit due date "
    "(\"by Friday\", \"before our exec sync next week\"). Never guess.\n"
    "      next_step_type: 'meeting' | 'phone_call' | 'email' | "
    "'document_send' | 'crm_update' | 'internal_loop_in' | 'other'\n"
    "      recommended_channel: 'email' | 'phone_call' | 'meeting' | "
    "'document_send' — the medium that best advances this item. "
    "Decide based on: factual vs nuanced (factual → email; "
    "decision/negotiation → phone), single deliverable (email) vs "
    "multi-party discussion (meeting), customer's verbal preference "
    "in the call (\"shoot me an email\" vs \"let's talk\"), urgency, "
    "rapport state.\n"
    "      channel_reasoning: str — one sentence explaining the channel "
    "choice in plain language, in neutral third person.\n"
    "      participants: list of {name: str, role: str, side: 'customer'"
    "|'vendor', source: 'named_in_call'|'mentioned_in_call'|"
    "'inferred_from_topic'} — every named person who should be on the "
    "next step, including specialists from the rep's own team who should "
    "be looped in based on the topics raised (e.g. 'Sales Engineer' "
    "for technical questions, 'Legal' for contract terms). Empty list "
    "is fine for solo follow-ups.\n"
    "      prep_artifacts: list of str — what the rep should prepare "
    "(deck slides, pricing tier sheet, customer's stated scale numbers, "
    "etc.). Empty list is fine.\n"
    "      email_draft: {subject: str, body: str} | null — populate when "
    "recommended_channel is 'email' or 'document_send'. Body in the "
    "rep's voice, ready to edit and send.\n"
    "      call_script: list of str | null — bullet talking points when "
    "recommended_channel is 'phone_call' or 'meeting'. Each bullet is "
    "one talking point, in the rep's voice.\n"
    "      implicit_signal: str | null — set when there's something the "
    "rep may not have noticed that drove this action item (a customer "
    "hesitation, a deferred question the rep didn't catch, a stakeholder "
    "the customer name-dropped without context). Plain language, "
    "third person. The rep should be able to read this and recognize "
    "what they missed. Null when the action item is obvious.\n"
    "      suggested_attachments: list of {title: str, reason: str} — "
    "suggested supporting documents the rep should attach when sending. "
    "Free-form titles describing what kind of document fits (e.g. "
    "\"API rate limit sheet\", \"Pricing tier overview\", \"Onboarding "
    "checklist\"). The system maps these to actual KB docs at the UI "
    "layer; the rep reviews and confirms before send. Empty list is "
    "fine when no document attachment is warranted.\n"
    "    }\n"
    "- coaching: {what_went_well: list[str], improvements: list[str], "
    "script_adherence_band: 'high' | 'medium' | 'low' | 'failing', "
    "compliance_gaps: list[str]} — phrase what_went_well and improvements "
    "as direct second-person notes to the rep (\"You did a great job "
    "framing…\", \"Next time, try…\"). script_adherence_band is a coarse "
    "bucket (the system converts it to a numeric score downstream).\n"
    "- follow_up_email_draft: {subject: str, body: str} — body written in "
    "the rep's voice, ready for them to edit and send\n"
    "- churn_risk_signal: 'high' | 'medium' | 'low' | 'none' — coarse "
    "bucket only. Do NOT emit a numeric churn_risk; the system maps the "
    "bucket to a calibrated number downstream.\n"
    "- upsell_signal: 'high' | 'medium' | 'low' | 'none' — coarse bucket "
    "only. Same downstream-mapping treatment as churn_risk_signal.\n"
    "- notable_snippets: list of {start_time: str, end_time: str, "
    "type: str, quality: 'positive'|'negative'|'neutral', title: str, "
    "description: str, tags: list[str]} — descriptions in neutral third "
    "person (\"This is where the customer pushed back on pricing.\")\n"
    "- inline_tags: list of {start_time: str, end_time: str, speaker: str, "
    "type: 'went_well' | 'improvement' | 'competitor' | 'commitment' | "
    "'objection_resolved' | 'objection_unresolved' | 'tense', "
    "popup_text: str, suggested_action: str | null} — per-moment tags meant "
    "for inline rendering on the transcript with a hover popup. popup_text "
    "is one short sentence of context; suggested_action is a one-line nudge "
    "or null. Empty list is fine when no taggable moments exist.\n"
    "- customer_signals: {commitment_language: list[str], "
    "change_talk: list[str], sustain_talk: list[str], "
    "trust_signals: list[str], urgency_language: list[str], "
    "objections: list of {quote: str, resolved: bool}} — verbatim "
    "customer-side quotes organized by signal type. Powers downstream "
    "behavior analysis. Empty lists are fine.\n"
    "- methodology_coverage: {framework: str, covered: list[str], "
    "missing: list[str], next_question: str | null} — when the tenant "
    "context specifies a sales or service methodology (SPIN, MEDDIC, "
    "structured-resolution), score which stages were covered and suggest "
    "one question that would address the most-missed stage. Default to "
    "{\"framework\": \"none\", \"covered\": [], \"missing\": [], "
    "\"next_question\": null} when no methodology is specified.\n"
    "- evidence: {objection_count: int, unresolved_objection_count: int, "
    "commitment_count: int, discovery_questions: int, "
    "competitor_mention_count: int} — counts of grounded events in the "
    "transcript. These are MEASUREMENTS (you observed them), not "
    "predictions. Be precise: only count items you can point at in the "
    "transcript. Zeros are honest answers; do not pad. The system uses "
    "these to compute deterministic rubric scores alongside the LLM "
    "buckets.\n\n"
    "Be thorough but concise. Ground every observation in evidence from the "
    "transcript. Never invent quotes. Keep the JSON schema exactly as "
    "specified."
)


# Subtrees / keys whose values are verbatim customer quotes from the
# transcript. Em-dashes inside these are preserved because they belong
# to the customer's actual speech, not to our analysis prose.
_VERBATIM_SUBTREES = {"customer_signals"}
_VERBATIM_VALUE_KEYS = {"quote"}


def _strip_dashes(obj: Any) -> None:
    """Recursively scrub em-dashes / en-dashes from analysis prose only.

    Field-aware: preserves em-dashes inside fields that hold verbatim
    customer quotes from the source transcript (the ``customer_signals``
    subtree, anything keyed ``quote``). Strips them from everything else.

    The replacement is '. ' (period + space) so sentence breaks read
    naturally; a leftover double-space gets collapsed.
    """
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in _VERBATIM_SUBTREES:
                continue  # preserve entire subtree as verbatim
            if k in _VERBATIM_VALUE_KEYS and isinstance(v, str):
                continue  # preserve this single value as verbatim
            if isinstance(v, str):
                obj[k] = _scrub_str(v)
            else:
                _strip_dashes(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i] = _scrub_str(v)
            else:
                _strip_dashes(v)


def _scrub_str(s: str) -> str:
    if "—" not in s and "–" not in s:
        return s
    out = s.replace(" — ", ". ").replace("—", ". ").replace(" – ", ". ").replace("–", ". ")
    # Collapse accidental double spaces created by the substitution.
    while "  " in out:
        out = out.replace("  ", " ")
    return out.strip()


def _dedup_action_items(result: Dict[str, Any]) -> None:
    """Merge action items that represent the same task split across
    multiple channels.

    The model sometimes emits two items for one underlying intent:
    one with ``recommended_channel='meeting'`` and one with
    ``recommended_channel='email'`` for the same downstream goal.
    The rep doesn't have two tasks; they have one task with two
    execution options. Walk the action_items list, group by a
    normalized title signature, and merge each group into a single
    item that carries both channels' artifacts (email_draft + call_script).

    The prompt now tells the model not to do this, but reps see the
    leftover dupes immediately when it slips; this is the safety net.
    """
    items = result.get("action_items")
    if not isinstance(items, list) or len(items) < 2:
        return

    # Keyword extraction: lowercase, drop punctuation, drop common verbs
    # and stopwords, return the remaining content tokens as a set. Two
    # items are considered the same intent when they share ≥3 content
    # tokens. This catches the common "Send the ROI model" vs "Build
    # ROI model" split that the model emits when it sees two channels
    # for the same task.
    import re as _re

    _STOP = {
        # verbs the model uses interchangeably
        "send", "schedule", "set", "book", "share", "follow", "up", "deliver",
        "draft", "prepare", "loop", "in", "with", "build", "create", "make",
        "get", "give", "show", "discuss", "review", "walk", "through",
        "confirm", "do", "do", "have",
        # articles + prepositions
        "the", "a", "an", "to", "for", "of", "on", "at", "by", "via", "from",
        "and", "or", "as", "with", "this", "that", "these", "those", "it",
        "is", "are", "be", "before", "after", "during",
        # generic targets
        "next", "step", "follow-up", "followup",
    }

    def _tokens(item: Any) -> set:
        if not isinstance(item, dict):
            return set()
        title = str(item.get("title") or "")
        norm = _re.sub(r"[^a-z0-9 ]", " ", title.lower())
        return {w for w in norm.split() if w and w not in _STOP and len(w) > 2}

    item_tokens: List[set] = [_tokens(it) for it in items]
    primary_map: Dict[int, int] = {}  # secondary_idx → primary_idx
    drop_indices: set[int] = set()
    for i in range(len(items)):
        if i in drop_indices or i in primary_map:
            continue
        for j in range(i + 1, len(items)):
            if j in drop_indices:
                continue
            shared = item_tokens[i] & item_tokens[j]
            # Same-intent if they share 3+ content tokens, OR if the
            # smaller token set is fully contained in the larger and
            # the smaller has ≥2 tokens.
            smaller = min(len(item_tokens[i]), len(item_tokens[j]))
            if len(shared) >= 3 or (smaller >= 2 and shared == item_tokens[i] or shared == item_tokens[j]):
                primary_map[j] = i
                drop_indices.add(j)

    # Group secondaries by primary so we can fold them in.
    by_primary: Dict[int, List[int]] = {}
    for secondary, primary in primary_map.items():
        by_primary.setdefault(primary, []).append(secondary)

    for primary_idx, secondary_idxs in by_primary.items():
        primary = items[primary_idx]
        if not isinstance(primary, dict):
            continue
        for secondary_idx in secondary_idxs:
            secondary = items[secondary_idx]
            if not isinstance(secondary, dict):
                continue
            # Inherit email_draft / call_script from whichever side
            # had it. Don't clobber the primary's existing data.
            for k in ("email_draft", "call_script", "implicit_signal",
                      "suggested_attachments"):
                if not primary.get(k) and secondary.get(k):
                    primary[k] = secondary[k]
            # Concatenate prep_artifacts uniquely so the merged item
            # carries everything either variant suggested.
            merged_prep = list(primary.get("prep_artifacts") or [])
            for p in (secondary.get("prep_artifacts") or []):
                if p not in merged_prep:
                    merged_prep.append(p)
            if merged_prep:
                primary["prep_artifacts"] = merged_prep
            # Take the EARLIER due_date when both are populated.
            if secondary.get("due_date") and (
                not primary.get("due_date")
                or secondary["due_date"] < primary["due_date"]
            ):
                primary["due_date"] = secondary["due_date"]
            # Bump priority to the higher of the two (high > medium > low).
            _rank = {"high": 3, "medium": 2, "low": 1}
            if (
                _rank.get(secondary.get("priority", "medium"), 2)
                > _rank.get(primary.get("priority", "medium"), 2)
            ):
                primary["priority"] = secondary["priority"]
            drop_indices.add(secondary_idx)

    if drop_indices:
        result["action_items"] = [
            it for i, it in enumerate(items) if i not in drop_indices
        ]


def _recompute_evidence(result: Dict[str, Any]) -> None:
    """Replace model-emitted evidence counts with deterministic ones.

    The prompt frames evidence.* as MEASUREMENTS, but in practice the
    model was approximating: commitment_count off by one on multiple
    rows vs the actual length of customer_signals.commitment_language,
    unresolved_objection_count drifting from objections[].resolved.
    Count the lists ourselves so the numbers match exactly.

    ``discovery_questions`` is the one count we still trust the model
    on, because there is no corresponding list to derive it from.
    """
    cs = result.get("customer_signals") or {}
    ev = result.get("evidence")
    if not isinstance(ev, dict):
        ev = {}
        result["evidence"] = ev

    objs = cs.get("objections") or []
    ev["objection_count"] = len(objs)
    # Structured objections are dicts with ``resolved: bool``; legacy
    # rows that came back as bare strings are conservatively counted
    # as unresolved (we have no signal otherwise).
    ev["unresolved_objection_count"] = sum(
        1
        for o in objs
        if (isinstance(o, dict) and o.get("resolved") is False)
        or isinstance(o, str)
    )
    ev["commitment_count"] = len(cs.get("commitment_language") or [])
    ev["competitor_mention_count"] = len(result.get("competitor_mentions") or [])
    # discovery_questions: preserve the model's emission, default to 0.
    ev.setdefault("discovery_questions", 0)


# Phrases the prompt forbids but the model still sometimes emits. These
# are the over-justification / AI-tells that read as bot-speak in
# customer-facing copy and as ungrounded coinages in coaching prose. We
# don't auto-rewrite (would destroy meaning); we log so the user can see
# which rows are still leaking and iterate the prompt.
_BANNED_PHRASE_PATTERNS = [
    # Over-justification / AI tells
    "not after",  # the 'X before Y, not after Y' construction
    "i want to make sure",
    "i want to ensure",
    "just to be clear",
    "one ask from my side",
    "one quick ask",
    "in an effort to",
    "going forward",
    # Internal coinages we ask the model not to coin (these are the
    # AI's own phrasing, not the customer's). Distinct from anything
    # the customer literally said on the call, which is allowed
    # through quote / key_moment fields.
    "pain-to-cost",
    "sanity-check dispatcher",
    "qualified the pain",
    "surfaced the buying committee",
    # AI-coined closers that read as cute / over-punchy
    "talk thursday",
    "talk monday",
    "talk tuesday",
    "talk wednesday",
    "talk friday",
    "onward.",
    "onward,",
    # Stock fillers
    "it's important to",
    "it's worth noting",
    "remember to",
    "make sure to",
    "in conclusion",
]


# Rhetorical-contrast tic: "X, not Y" / "X — not Y" / "X rather than
# Y" / "X instead of Y" / "X as opposed to Y" used for emphasis.
# Catches multi-word Y up to ~6 words so "not assumed ones" /
# "not a skeptic" / "not a projection we invented" all fire.
_CONTRAST_TIC_PATTERNS = [
    # ", not [up to 6 words]" — most common form
    re.compile(r",\s+not\s+(?!only|just|merely|simply|necessarily|even|always|yet|sure\b)[a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){0,5}\b", re.IGNORECASE),
    # "— not [up to 6 words]" (em-dash or en-dash variant)
    re.compile(r"[—–]\s*not\s+[a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){0,5}\b", re.IGNORECASE),
    # " rather than [up to 5 words]"
    re.compile(r"\brather\s+than\s+[a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){0,4}\b", re.IGNORECASE),
    # " instead of [up to 5 words]" — slightly noisier so we require
    # a clean lowercase tail (skips proper nouns like "instead of
    # Salesforce")
    re.compile(r"\binstead\s+of\s+[a-z][a-z'\-]*(?:\s+[a-z][a-z'\-]*){0,4}\b", re.IGNORECASE),
    # " as opposed to "
    re.compile(r"\bas\s+opposed\s+to\b", re.IGNORECASE),
]


# All-caps section labels and dashed dividers in artifact bodies.
# Three uppercase letters or more followed by a colon ("EOD:", "CRM:",
# "AGENDA:"), or a "--- HEADER ---" dashed divider. Skips legitimate
# acronyms in running prose by requiring the colon or dashes.
_ALLCAPS_LABEL_PATTERNS = [
    re.compile(r"^\s*[-=*]{2,}\s*[A-Z][A-Z0-9 _'/&]+[-=*]{2,}\s*$", re.MULTILINE),
    re.compile(r"^\s*[A-Z]{3,}[A-Z0-9 _'/&]{0,40}:\s*$", re.MULTILINE),
]


def _log_jargon_hits(result: Dict[str, Any], interaction_id: Optional[str] = None) -> None:
    """Walk the analysis output and log when banned phrases slip through.

    Field-aware: skips ``customer_signals`` and any ``quote`` value
    (verbatim customer text is allowed to contain anything). Everywhere
    else, if a banned phrase appears in any string value, emit a warning
    with the field path so we can iterate the prompt without flying blind.

    Also catches the rhetorical-contrast tic ("X, not Y" / "X rather
    than Y") and all-caps section labels in artifact bodies. These are
    structural AI tells that don't reduce to a literal phrase list.
    """
    hits: List[str] = []

    def _walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _VERBATIM_SUBTREES:
                    continue
                if k in _VERBATIM_VALUE_KEYS and isinstance(v, str):
                    continue
                _walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            lower = obj.lower()
            for phrase in _BANNED_PHRASE_PATTERNS:
                if phrase in lower:
                    hits.append(f"{path}: '{phrase}'")
            for pat in _CONTRAST_TIC_PATTERNS:
                m = pat.search(obj)
                if m:
                    hits.append(f"{path}: contrast-tic '{m.group(0).strip()}'")
            for pat in _ALLCAPS_LABEL_PATTERNS:
                m = pat.search(obj)
                if m:
                    hits.append(f"{path}: allcaps-label '{m.group(0).strip()}'")

    _walk(result, "")
    if hits:
        logger.warning(
            "Analysis jargon leak (%s): %d hit(s) - %s",
            interaction_id or "unknown",
            len(hits),
            "; ".join(hits[:10]),
        )


# Placeholder timestamp values the model emits when the source transcript
# has no real segment timestamps. The frontend renders inline overlays
# anchored to these times; bogus zeros pin every tag to the start of the
# call, which is worse than just hiding them.
_ZERO_TIMESTAMPS = {"00:00", "0:00", "00:00:00", "0:00:00", ""}


def _scrub_zero_timestamps(result: Dict[str, Any]) -> None:
    """Convert placeholder ``"00:00-00:00"`` timestamps to None.

    Applies to per-moment fields the UI uses to anchor overlays
    (inline_tags, notable_snippets, key_moments). Leaves real
    timestamps alone. Only triggers when BOTH start and end are zero
    on the same item; partial-zero anchors are kept (a snippet from
    the very start of a call genuinely begins at 00:00).
    """
    for field in ("inline_tags", "notable_snippets", "key_moments"):
        items = result.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            start_zero = str(item.get("start_time", "")).strip() in _ZERO_TIMESTAMPS
            end_zero = str(item.get("end_time", "")).strip() in _ZERO_TIMESTAMPS
            if start_zero and end_zero:
                item["start_time"] = None
                item["end_time"] = None
            # ``key_moments`` also has a ``time`` shorthand field.
            if field == "key_moments":
                if str(item.get("time", "")).strip() in _ZERO_TIMESTAMPS:
                    item["time"] = None


def _format_transcript(
    segments: List[Dict[str, Any]],
    inline_tags: Optional[Dict[int, str]] = None,
) -> str:
    """Convert transcript segments to a readable string.

    When ``inline_tags`` is provided, the per-segment-index tag string
    (already pre-formatted by ``paralinguistic_prompt`` as ``"[pitch
    ↑1.8σ · pause-before 1.6σ]"``) gets appended after the time/
    speaker prefix on the matching turn. Absent indices render
    bit-identical to the no-tag path — callers can pass ``None`` to
    short-circuit the lookup entirely.
    """
    lines: List[str] = []
    for idx, seg in enumerate(segments):
        time = seg.get("time", seg.get("start_time", "00:00"))
        speaker = seg.get("speaker", "Unknown")
        text = seg.get("text", "")
        tag = inline_tags.get(idx) if inline_tags else None
        if tag:
            lines.append(f"[{time}] {speaker} {tag}: {text}")
        else:
            lines.append(f"[{time}] {speaker}: {text}")
    return "\n".join(lines)


# ── Per-domain prompt registry (added with ``dom_001``) ────────────────
#
# Today every domain dispatches to ``ANALYSIS_SYSTEM_PROMPT`` (the
# sales-coach prompt). PRs 3 and 4 will swap in real CS and IT-Support
# prompts; until they land, every motion gets analyzed by the existing
# prompt, which is the safe behaviour because the sales prompt is the
# one production-validated rubric we have.
#
# Keeping the registry explicit (rather than a generic ``getattr`` lookup)
# means a typo in a domain name fails loud at import time instead of
# silently degrading the wrong call type into the wrong rubric.
ANALYSIS_SYSTEM_PROMPT_BY_DOMAIN: Dict[str, str] = {
    "sales": ANALYSIS_SYSTEM_PROMPT,
    "customer_service": ANALYSIS_SYSTEM_PROMPT,
    "it_support": ANALYSIS_SYSTEM_PROMPT,
    "generic": ANALYSIS_SYSTEM_PROMPT,
}


def _system_prompt_for_domain(domain: Optional[str]) -> str:
    """Return the analysis system prompt for an interaction's ``domain``.

    NULL / unknown / legacy values fall back to the sales prompt — the
    one rubric we know is production-safe. PRs 3 and 4 will land
    domain-specific prompts; the dispatch site (``AIAnalysisService.analyze``)
    is already wired so flipping the dict entry is a one-line change.
    """
    if not domain:
        return ANALYSIS_SYSTEM_PROMPT
    return ANALYSIS_SYSTEM_PROMPT_BY_DOMAIN.get(domain, ANALYSIS_SYSTEM_PROMPT)


class AIAnalysisService:
    """Run deep AI analysis on call transcripts."""

    def __init__(self) -> None:
        self._client = get_async_anthropic()

    async def analyze(
        self,
        transcript_segments: List[Dict[str, Any]],
        tier: str = "sonnet",
        triage_result: Optional[Dict[str, Any]] = None,
        system_prompt_override: Optional[str] = None,
        tenant_context_block: Optional[str] = None,
        rag_context_block: Optional[str] = None,
        max_tokens_override: Optional[int] = None,
        tenant_context: Optional[Dict[str, Any]] = None,
        customer_brief: Optional[Dict[str, Any]] = None,
        paralinguistic_block: Optional[Any] = None,
        complexity_score: Optional[float] = None,
        call_date: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyze a transcript and return structured insights.

        Parameters
        ----------
        transcript_segments:
            List of dicts with keys ``time``/``start_time``, ``speaker``, ``text``.
        tier:
            ``"haiku"`` for simple calls, ``"sonnet"`` for complex calls.
        triage_result:
            Optional output from :class:`TriageService` to give the model context.
        system_prompt_override:
            If provided, used in place of ``ANALYSIS_SYSTEM_PROMPT`` (prompt-variant swap).
        tenant_context_block:
            Pre-formatted tenant block appended to the user message. Takes
            precedence over ``tenant_context`` when provided.
        rag_context_block:
            Optional knowledge-base excerpts retrieved for this specific call.
        max_tokens_override:
            Per-tenant parameter override for ``max_tokens``.
        tenant_context:
            Raw tenant brief dict; auto-formatted and injected as a cacheable
            system block when ``tenant_context_block`` is not provided.
        customer_brief:
            Raw customer brief dict; auto-formatted as a system block.
        """
        model = MODELS.get(tier, MODELS["sonnet"])
        # Phase 2: paralinguistic block contributes both a structured
        # prompt section and a per-turn-index inline tag map. When the
        # tenant flag is off, the audio is unavailable, or the
        # extractor returns ``available: False``, ``paralinguistic_block``
        # is None and the formatted transcript is bit-identical to the
        # pre-Phase-2 output. Decision Q3: silent fallback.
        inline_tags = (
            getattr(paralinguistic_block, "inline_tags", None)
            if paralinguistic_block is not None
            else None
        )
        para_structured = (
            getattr(paralinguistic_block, "structured_text", "")
            if paralinguistic_block is not None
            else ""
        )
        formatted = _format_transcript(transcript_segments, inline_tags)
        # Domain dispatch: pick the per-motion prompt unless the caller
        # passed an explicit override (prompt-variant A/B swap). When
        # ``domain`` is omitted (legacy callers, NULL on the row), the
        # sales prompt is used — same behaviour as before ``dom_001``.
        system_prompt = system_prompt_override or _system_prompt_for_domain(domain)

        # Build user message, optionally prepending triage + tenant + RAG context.
        parts: List[str] = []
        # Call-date anchor. The prompt asks the model to resolve weekday
        # references ('Thursday', 'tomorrow') to YYYY-MM-DD due_dates; that
        # only works if it knows when the call happened. Without this
        # anchor, due_date stays null even when the customer literally
        # said 'Thursday' on the call.
        if call_date:
            parts.append(
                f"## Call Date\n"
                f"This call took place on {call_date}. Resolve any "
                f"weekday or relative-date references in the transcript "
                f"('Thursday', 'tomorrow', 'next week') to specific "
                f"YYYY-MM-DD dates relative to this anchor when populating "
                f"action_items[].due_date.\n"
            )
        if triage_result:
            summary = triage_result.get("quick_summary", "")
            topics = ", ".join(triage_result.get("topics", []))
            parts.append(
                f"## Triage Context\n"
                f"Quick summary: {summary}\n"
                f"Detected topics: {topics}\n"
            )
        if tenant_context_block:
            parts.append(tenant_context_block)
        if rag_context_block:
            parts.append(rag_context_block)
        if para_structured:
            parts.append(para_structured)
        parts.append(f"## Transcript\n{formatted}")
        user_content = "\n".join(parts)

        raw_text = ""

        # Assemble system blocks. Order matters for prompt caching: Anthropic
        # caches request prefixes, so the MOST stable content goes first to
        # maximize hit rate across calls.
        #
        # Order (most-stable to least-stable):
        #   1. Analyst instructions (system_prompt). Identical for every
        #      analysis call platform-wide — highest hit rate.
        #   2. Tenant context. Stable within a tenant, varies across tenants.
        #   3. Customer brief. Varies per customer (lowest hit rate).
        #
        # Three cache breakpoints (well under Anthropic's max of 4). The user
        # message (transcript) is not cached because it's unique per call.
        system_blocks: List[Dict[str, Any]] = []
        system_blocks.append(
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        )
        tenant_text = (
            None
            if tenant_context_block  # already appended to user message above
            else format_brief_for_prompt(tenant_context or {})
        )
        if tenant_text:
            system_blocks.append(
                {
                    "type": "text",
                    "text": tenant_text,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        customer_text = format_customer_brief_for_prompt(customer_brief or {})
        if customer_text:
            system_blocks.append(
                {
                    "type": "text",
                    "text": customer_text,
                    "cache_control": {"type": "ephemeral"},
                }
            )

        # Tiered max_tokens: cheaper baseline for simple calls; full ceiling
        # for high-complexity main analysis or long inputs. Explicit overrides
        # are honored but capped to the tier's hard ceiling.
        approx_input_tokens = (
            sum(len(b.get("text", "")) for b in system_blocks) + len(user_content)
        ) // 4
        budget = compute_max_tokens(
            tier,
            input_tokens=approx_input_tokens,
            task_type="main_analysis",
            complexity_score=complexity_score,
            explicit_override=max_tokens_override,
        )

        try:
            t0 = time.perf_counter()
            response = await self._client.messages.create(
                model=model,
                max_tokens=budget,
                system=system_blocks,
                messages=[{"role": "user", "content": user_content}],
            )
            _metrics.LLM_LATENCY.labels(surface="analysis", model=model).observe(
                time.perf_counter() - t0
            )

            raw_text = response.content[0].text
            stop_reason = response.stop_reason

            # Retry-on-truncation: when stop_reason='max_tokens' and the
            # caller didn't explicitly cap us, retry once with double
            # the budget. The terse prompt should keep most calls under
            # 8K; this is the safety net for the long-tail mega-calls
            # (90+ min enterprise stuff) that still overflow. We pay the
            # cost of the second call only on the rare truncation case;
            # the first call's output tokens we paid for either way.
            retried = False
            if (
                stop_reason == "max_tokens"
                and max_tokens_override is None
                and budget < 16384
            ):
                retry_budget = min(budget * 2, 16384)
                logger.warning(
                    "AI analysis truncated at %d tokens; retrying once with budget=%d",
                    budget, retry_budget,
                )
                t1 = time.perf_counter()
                response = await self._client.messages.create(
                    model=model,
                    max_tokens=retry_budget,
                    system=system_blocks,
                    messages=[{"role": "user", "content": user_content}],
                )
                _metrics.LLM_LATENCY.labels(
                    surface="analysis_retry", model=model
                ).observe(time.perf_counter() - t1)
                raw_text = response.content[0].text
                stop_reason = response.stop_reason
                budget = retry_budget
                retried = True
                if stop_reason == "max_tokens":
                    logger.warning(
                        "AI analysis STILL truncated after retry at %d chars (budget=%d)",
                        len(raw_text), budget,
                    )

            # Stamp every result with stop_reason + raw response length
            # so we have postmortem visibility without log access. When
            # we see ``_stop_reason='max_tokens'`` AND a low _raw_chars
            # we know the cap is firing; when stop_reason='end_turn'
            # but parse fails we know the issue is malformed JSON not
            # truncation.
            stamp = {
                "_stop_reason": stop_reason,
                "_raw_chars": len(raw_text),
                "_max_tokens_budget": budget,
                "_retried": retried,
            }
            if stop_reason == "max_tokens":
                logger.warning(
                    "AI analysis hit max_tokens — output truncated at %d chars (budget=%d)",
                    len(raw_text), budget,
                )
            cleaned = _strip_json_fences(raw_text)
            try:
                result: Dict[str, Any] = json.loads(cleaned)
                _strip_dashes(result)
                _dedup_action_items(result)
                _recompute_evidence(result)
                _scrub_zero_timestamps(result)
                _log_jargon_hits(result)
                result.update(stamp)
                return result
            except json.JSONDecodeError as parse_exc:
                # Best-effort repair: truncated responses (max_tokens cut off
                # the model mid-emit) leave dangling strings / arrays /
                # objects that ``json-repair`` can stitch closed. We accept
                # the partial result rather than leaving every long-form
                # call with empty insights.
                logger.warning(
                    "AI analysis JSON parse failed (%s); attempting repair",
                    parse_exc,
                )
                try:
                    from json_repair import repair_json  # type: ignore
                    repaired = repair_json(cleaned, return_objects=True)
                    if isinstance(repaired, dict) and repaired:
                        repaired.setdefault("_recovered", True)
                        _strip_dashes(repaired)
                        _dedup_action_items(repaired)
                        _recompute_evidence(repaired)
                        _scrub_zero_timestamps(repaired)
                        _log_jargon_hits(repaired)
                        repaired.update(stamp)
                        return repaired
                except Exception as repair_exc:
                    logger.error("json-repair fallback failed: %s", repair_exc)
                # Final fallback — preserve the raw text in summary so the
                # row isn't completely empty.
                fallback = {
                    "summary": raw_text,
                    "error": f"JSON parse error: {parse_exc}",
                }
                fallback.update(stamp)
                return fallback
        except anthropic.APIError as exc:
            logger.error("Anthropic API error during analysis: %s", exc)
            return {"error": f"Anthropic API error: {exc}"}
