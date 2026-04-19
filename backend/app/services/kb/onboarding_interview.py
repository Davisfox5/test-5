"""LINDA onboarding interview agent.

A stateful conversational agent that walks a tenant admin through a
structured interview to populate the onboarding-owned sections of the
tenant brief — ``goals``, ``kpis``, ``strategies``, ``org_structure``,
``personal_touches``.

Design:

* **Turn-driven**. Each call to ``step()`` takes the user's most recent
  reply (empty string for the very first call) and returns the next
  assistant message plus the updated session state.
* **Haiku-driven extraction**. After every user turn, we ask Haiku to
  (a) extract any concrete values from the reply into the structured
  brief shape, and (b) decide which section to probe next.
* **Persisted state**. Session rows live in the ``onboarding_sessions``
  table and can be resumed across browser reloads. On completion we
  splice the collected fields into ``Tenant.tenant_context`` via
  ``PUT /admin/tenant-context/fields`` semantics.
* **Stateless callers**. The agent doesn't mutate the tenant row until
  the session is marked completed; a cancelled interview just leaves
  behind a row with ``status="abandoned"`` that admins can inspect.

This module exposes two things:

* :class:`OnboardingInterview` — the service class with ``start`` and
  ``step`` methods.
* :func:`merge_answers_into_brief` — the pure helper that takes a
  collected-answers dict and merges it into an existing tenant brief,
  used by both the completion path and by the Infer-From-Sources agent
  when it learns the same fields passively.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import Tenant
from backend.app.services.kb.context_builder import (
    _empty_personal_touches,
    _validate_brief,
)

logger = logging.getLogger(__name__)


_MODEL = "claude-haiku-4-5-20251001"

# Sections the interview is responsible for. The KB-derived and
# learned/playbook sections are owned by other agents and are not touched
# here. Order here is the default probing order.
_TARGET_SECTIONS = [
    "goals",
    "kpis",
    "strategies",
    "org_structure",
    "personal_touches",
]


# ── Prompts ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are LINDA's onboarding interviewer. Your job is to have a short, "
    "natural conversation with a new tenant admin so that LINDA can learn "
    "how their business works. Keep each question one sentence — two if "
    "absolutely necessary — and focus on one topic at a time. Acknowledge "
    "their previous answer briefly before asking the next thing. Never ask "
    "for something you've already collected.\n\n"
    "You are ALSO responsible for extracting structured values from every "
    "reply, and deciding when you have enough.\n\n"
    "Sections to cover (in rough order):\n"
    "1. goals — their top 2-3 business goals for the next 90 days.\n"
    "2. kpis — the metrics they track and their targets (name + target,\n"
    "   current is optional).\n"
    "3. strategies — 2-4 sentences on how they win business today.\n"
    "4. org_structure — teams, escalation paths, and territories if any.\n"
    "5. personal_touches — greeting / sign-off style, phrasing "
    "preferences, rituals, humor level, pacing, phrases to avoid.\n\n"
    "Respond in JSON only (no markdown fences) with this shape:\n"
    "{\n"
    '  "assistant_message": "what to say to the tenant next",\n'
    '  "updated_answers": { /* partial brief — only fields you are confident '
    "about right now. Preserve keys you already collected. */ },\n"
    '  "next_section": "goals|kpis|strategies|org_structure|personal_touches"'
    " or null,\n"
    '  "completed_sections": ["goals", "kpis"],\n'
    '  "done": true|false  // true only when you have usable data for all '
    "target sections or the user explicitly asks to stop\n"
    "}\n\n"
    "Rules:\n"
    "- Never invent facts the user didn't say. Leave fields empty when "
    "unsure.\n"
    "- When a section is completed, move on — don't revisit unless the user "
    "circles back.\n"
    "- Keep the whole answer under ~250 words. The assistant_message "
    "should typically be 1-3 sentences.\n"
    "- On the very first turn (empty user reply), introduce yourself in one "
    "short paragraph and ask about goals."
)


# ── Helpers ────────────────────────────────────────────────────────────


def _empty_answers() -> Dict[str, Any]:
    """The shape we collect during the interview, matching the onboarding-
    owned subset of ``tenant_context``."""
    return {
        "goals": [],
        "kpis": [],
        "strategies": [],
        "org_structure": {},
        "personal_touches": _empty_personal_touches(),
    }


def _coerce_answers(data: Any) -> Dict[str, Any]:
    """Normalise whatever the model returns into the answers shape.

    Partial updates are OK — missing keys get left alone. Wrong types get
    dropped silently so a malformed turn doesn't corrupt the state.
    """
    base = _empty_answers()
    if not isinstance(data, dict):
        return base

    goals = data.get("goals")
    if isinstance(goals, list):
        base["goals"] = [str(g)[:300] for g in goals if g][:8]

    kpis = data.get("kpis")
    if isinstance(kpis, list):
        cleaned: List[Dict[str, Any]] = []
        for k in kpis[:10]:
            if isinstance(k, dict):
                cleaned.append(
                    {
                        "name": str(k.get("name", ""))[:100],
                        "target": k.get("target"),
                        "current": k.get("current"),
                    }
                )
        base["kpis"] = cleaned

    strategies = data.get("strategies")
    if isinstance(strategies, list):
        base["strategies"] = [str(s)[:400] for s in strategies if s][:8]

    org = data.get("org_structure")
    if isinstance(org, dict):
        base["org_structure"] = {
            "teams": [str(t)[:100] for t in (org.get("teams") or []) if t][:10],
            "escalation_path": [
                str(e)[:120] for e in (org.get("escalation_path") or []) if e
            ][:10],
            "territories": [
                str(t)[:120] for t in (org.get("territories") or []) if t
            ][:20],
        }

    pt = data.get("personal_touches")
    if isinstance(pt, dict):
        merged = _empty_personal_touches()
        for key in merged:
            if key in pt:
                merged[key] = pt[key]
        base["personal_touches"] = merged

    return base


def _merge_answers(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Union-merge ``new`` into ``existing``. Non-empty new values win."""
    out = dict(existing)
    for key in _TARGET_SECTIONS:
        new_val = new.get(key)
        if new_val in (None, "", [], {}):
            continue
        if isinstance(new_val, list) and isinstance(out.get(key), list):
            # Dedupe while preserving order; list-of-dict by JSON identity.
            seen: set = set()
            merged: List[Any] = []
            for item in list(out.get(key) or []) + list(new_val):
                key_repr = json.dumps(item, sort_keys=True, default=str)
                if key_repr in seen:
                    continue
                seen.add(key_repr)
                merged.append(item)
            out[key] = merged
        elif isinstance(new_val, dict) and isinstance(out.get(key), dict):
            sub = dict(out.get(key) or {})
            for k, v in new_val.items():
                if v in (None, "", [], {}):
                    continue
                sub[k] = v
            out[key] = sub
        else:
            out[key] = new_val
    return out


def merge_answers_into_brief(
    brief: Dict[str, Any],
    answers: Dict[str, Any],
) -> Dict[str, Any]:
    """Splice onboarding answers into an existing tenant brief.

    Used both at end-of-interview and by the Infer-From-Sources agent when
    it passively confirms a value. The KB-derived and learned sections are
    left untouched.
    """
    base = _validate_brief(brief or {})
    for key in _TARGET_SECTIONS:
        incoming = answers.get(key)
        if incoming in (None, "", [], {}):
            continue
        base[key] = incoming
    return base


# ── Agent ──────────────────────────────────────────────────────────────


@dataclass
class InterviewTurn:
    """The result of one ``step()`` call."""

    assistant_message: str
    done: bool
    next_section: Optional[str]
    completed_sections: List[str]
    answers: Dict[str, Any]
    history: List[Dict[str, str]]


class OnboardingInterview:
    """Stateful interview agent."""

    def __init__(self, client: Optional[anthropic.AsyncAnthropic] = None) -> None:
        if client is not None:
            self._client = client
        else:
            settings = get_settings()
            self._client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    @staticmethod
    def new_state() -> Dict[str, Any]:
        """Initial session state. Persist this JSON alongside the tenant."""
        return {
            "history": [],  # list of {"role": "user"|"assistant", "content": "..."}
            "answers": _empty_answers(),
            "completed_sections": [],
            "next_section": _TARGET_SECTIONS[0],
            "done": False,
        }

    async def step(
        self,
        state: Dict[str, Any],
        user_reply: str = "",
    ) -> InterviewTurn:
        """Advance the interview by one turn.

        Pass an empty ``user_reply`` on the very first call to get the
        opening message.
        """
        history = list(state.get("history") or [])
        answers = dict(state.get("answers") or _empty_answers())
        completed = list(state.get("completed_sections") or [])

        if user_reply.strip():
            history.append({"role": "user", "content": user_reply.strip()[:2000]})

        # Build the user message that gives Haiku everything it needs to
        # decide what to say and what to extract.
        state_block = json.dumps(
            {
                "answers_so_far": answers,
                "completed_sections": completed,
                "next_section_hint": state.get("next_section") or _TARGET_SECTIONS[0],
            },
            default=str,
        )
        transcript_block = "\n".join(
            f"{t['role']}: {t['content']}" for t in history[-12:]
        )

        user_message = (
            "## Interview state\n"
            f"{state_block}\n\n"
            "## Recent transcript\n"
            f"{transcript_block or '(no turns yet)'}"
            + ("\n\n## Latest user reply\n" + user_reply.strip() if user_reply.strip() else "")
        )

        try:
            resp = await self._client.messages.create(
                model=_MODEL,
                max_tokens=800,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw = resp.content[0].text
            data = json.loads(raw)
        except (anthropic.APIError, json.JSONDecodeError, IndexError, KeyError):
            logger.exception("OnboardingInterview Haiku call failed")
            return InterviewTurn(
                assistant_message=(
                    "Sorry, I had trouble just then — could you repeat that?"
                ),
                done=False,
                next_section=state.get("next_section") or _TARGET_SECTIONS[0],
                completed_sections=completed,
                answers=answers,
                history=history,
            )

        assistant_message = str(data.get("assistant_message", "")).strip() or (
            "Tell me a bit about your goals for the next 90 days."
        )
        updated_fragment = _coerce_answers(data.get("updated_answers") or {})
        answers = _merge_answers(answers, updated_fragment)

        new_completed = data.get("completed_sections")
        if isinstance(new_completed, list):
            completed = sorted(
                set(completed)
                | {s for s in new_completed if s in _TARGET_SECTIONS}
            )

        raw_next = data.get("next_section")
        next_section = raw_next if raw_next in _TARGET_SECTIONS else None
        done = bool(data.get("done", False))

        history.append({"role": "assistant", "content": assistant_message})

        return InterviewTurn(
            assistant_message=assistant_message,
            done=done,
            next_section=next_section,
            completed_sections=completed,
            answers=answers,
            history=history,
        )

    @staticmethod
    def update_state(state: Dict[str, Any], turn: InterviewTurn) -> Dict[str, Any]:
        """Fold a turn's result back into the persistable state dict."""
        return {
            "history": turn.history,
            "answers": turn.answers,
            "completed_sections": turn.completed_sections,
            "next_section": turn.next_section,
            "done": turn.done,
        }


# ── Persisting the completed interview onto the tenant ─────────────────


async def apply_completed_interview(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    answers: Dict[str, Any],
) -> Dict[str, Any]:
    """Splice the interview answers into the tenant's brief and persist."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError(f"Tenant {tenant_id} not found")
    brief = merge_answers_into_brief(tenant.tenant_context or {}, answers)
    brief["updated_at"] = datetime.now(timezone.utc).isoformat()
    tenant.tenant_context = brief
    return brief
