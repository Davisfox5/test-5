"""Pure helpers for the cold-outreach engine.

No DB, no HTTP, no LLM — everything here is deterministic and unit-testable,
shared by the API router (async) and the Celery scheduler/ingest hooks (sync).
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

# ── Pipeline statuses ───────────────────────────────────────────────────

PIPELINE_STATUSES = (
    "new",
    "queued",
    "contacted",
    "replied",
    "demo",
    "won",
    "lost",
    "do_not_contact",
)

# Ranking used to keep automatic transitions monotonic: a campaign event
# never moves a prospect *backwards* (a bump send must not demote a
# prospect the rep already advanced to "demo"). Manual PATCH may set any
# status; do_not_contact wins over everything.
_STATUS_RANK = {s: i for i, s in enumerate(PIPELINE_STATUSES)}

TERMINAL_STATUSES = ("won", "lost", "do_not_contact")


def advance_status(current: Optional[str], proposed: str) -> Optional[str]:
    """Return the status to write for an *automatic* transition, or None.

    - never leaves a terminal status
    - never moves backwards (rank-monotonic)
    - do_not_contact always applies
    """
    if proposed == "do_not_contact":
        return proposed
    if current in TERMINAL_STATUSES:
        return None
    if current is not None and _STATUS_RANK.get(proposed, 0) <= _STATUS_RANK.get(current, -1):
        return None
    return proposed


# ── Campaign config ─────────────────────────────────────────────────────


class OutreachStep(BaseModel):
    """One touch in the sequence. ``offset_days`` is measured from the
    previous touch (0 for the first)."""

    offset_days: int = Field(0, ge=0, le=90)
    # Optional per-step guidance appended to the drafting prompt
    # (e.g. "short bump, reference the original email").
    guidance: Optional[str] = None


class OutreachTemplate(BaseModel):
    """Base template + the CAN-SPAM identity block.

    ``subject``/``body`` are the starting point the per-prospect drafts
    personalize from; ``{business_name}``-style placeholders are allowed
    and are substituted before the LLM sees the text. The identity fields
    are required before a campaign can activate — they render into the
    footer of every send.
    """

    subject: str = Field(..., min_length=1, max_length=400)
    body: str = Field(..., min_length=1)
    sender_name: str = Field(..., min_length=1, max_length=200)
    sender_business: str = Field(..., min_length=1, max_length=200)
    # CAN-SPAM: a valid physical postal address of the sender.
    physical_address: str = Field(..., min_length=1, max_length=500)


class SendWindow(BaseModel):
    """Local-time window inside which sends may fire."""

    start_hour: int = Field(9, ge=0, le=23)
    end_hour: int = Field(17, ge=1, le=24)
    timezone: str = "America/New_York"
    # ISO weekday numbers, 1=Mon … 7=Sun.
    days: List[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])

    @field_validator("timezone")
    @classmethod
    def _tz_exists(cls, v: str) -> str:
        ZoneInfo(v)  # raises on unknown key
        return v

    @field_validator("days")
    @classmethod
    def _days_valid(cls, v: List[int]) -> List[int]:
        if not v or any(d < 1 or d > 7 for d in v):
            raise ValueError("days must be non-empty ISO weekdays 1-7")
        return sorted(set(v))


class OutreachConfig(BaseModel):
    """Validated shape of ``Campaign.config`` for kind='outreach'."""

    template: OutreachTemplate
    send_window: SendWindow = Field(default_factory=SendWindow)
    steps: List[OutreachStep] = Field(
        default_factory=lambda: [
            OutreachStep(offset_days=0),
            OutreachStep(offset_days=4, guidance="Short, friendly bump."),
        ],
        min_length=1,
        max_length=6,
    )
    # None → settings.OUTREACH_DEFAULT_DAILY_LIMIT at send time.
    daily_limit: Optional[int] = Field(None, ge=1, le=200)
    max_touches: int = Field(3, ge=1, le=6)
    # review: drafts wait for human approval. auto: drafts queue
    # themselves as soon as they generate.
    mode: str = "review"
    # Preferred provider; None → google-then-microsoft fallback.
    provider: Optional[str] = None

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        if v not in ("review", "auto"):
            raise ValueError("mode must be 'review' or 'auto'")
        return v

    @field_validator("provider")
    @classmethod
    def _provider_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("google", "microsoft"):
            raise ValueError("provider must be 'google' or 'microsoft'")
        return v


def parse_config(raw: dict) -> OutreachConfig:
    """Validate a stored/incoming config dict. Raises pydantic ValidationError."""
    return OutreachConfig.model_validate(raw or {})


# ── Domain normalization (import idempotency key) ───────────────────────

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def normalize_domain(website: Optional[str]) -> Optional[str]:
    """Reduce a website/URL/domain string to a bare registrable host.

    ``https://www.Foo-Gym.com/pricing?x=1`` → ``foo-gym.com``. Returns
    None when nothing domain-shaped survives — the import falls back to
    name-based matching for those rows.
    """
    if not website:
        return None
    s = website.strip().lower()
    s = _SCHEME_RE.sub("", s)
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.split("@")[-1]  # tolerate pasted mailto:/user@host forms
    s = s.split(":", 1)[0]  # strip port
    if s.startswith("www."):
        s = s[4:]
    s = s.strip(".")
    if "." not in s or " " in s or not s:
        return None
    return s


# ── Opt-out detection ───────────────────────────────────────────────────

# Deliberately conservative: these mark the prospect do-not-contact and
# halt every sequence, so false positives are worse than false negatives.
# Soft negatives ("not interested") end the sequence via the normal reply
# path where a human (or the brief) decides what's next.
_STOP_PATTERNS = [
    r"\bunsubscribe\b",
    r"\bopt[ -]?out\b",
    r"\bopt (?:me|us) out\b",
    r"\bremove me\b",
    r"\btake me off\b",
    r"\bstop (?:e[- ]?mail|email|contact|messag)\w*\b",
    r"\bdo not (?:e[- ]?mail|email|contact)\b",
    r"\bdon'?t (?:e[- ]?mail|email|contact)\b",
    r"^\s*stop\s*[.!]?\s*$",
]
_STOP_RE = re.compile("|".join(_STOP_PATTERNS), re.IGNORECASE | re.MULTILINE)


def detect_opt_out(text: Optional[str]) -> bool:
    """True when a reply body reads as an opt-out request."""
    if not text:
        return False
    # Only scan the top of the message — quoted history below the reply
    # contains OUR footer ("Reply STOP…"), which must not self-trigger.
    head = "\n".join(text.splitlines()[:15])
    return bool(_STOP_RE.search(head))


# ── Bounce (DSN) heuristics ─────────────────────────────────────────────

_BOUNCE_FROM_RE = re.compile(
    r"(mailer-daemon|postmaster|mail delivery (?:subsystem|system))",
    re.IGNORECASE,
)
_MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")


def looks_like_bounce(from_address: Optional[str], subject: Optional[str]) -> bool:
    if from_address and _BOUNCE_FROM_RE.search(from_address):
        return True
    if subject and re.search(
        r"(undeliverable|delivery status notification|failure notice|"
        r"returned mail|mail delivery failed)",
        subject,
        re.IGNORECASE,
    ):
        return True
    return False


def extract_message_ids(text: Optional[str], limit: int = 20) -> List[str]:
    """Candidate RFC-822 Message-IDs embedded in a DSN body."""
    if not text:
        return []
    return _MSGID_RE.findall(text)[:limit]


# ── Send window / footer ────────────────────────────────────────────────


def in_send_window(window: SendWindow, now_utc: Optional[datetime] = None) -> bool:
    now_utc = now_utc or datetime.now(timezone.utc)
    local = now_utc.astimezone(ZoneInfo(window.timezone))
    if local.isoweekday() not in window.days:
        return False
    start = time(hour=window.start_hour)
    end = time(hour=window.end_hour - 1, minute=59, second=59) if window.end_hour < 24 else time(23, 59, 59)
    return start <= local.time() <= end


def local_day_bounds_utc(
    window: SendWindow, now_utc: Optional[datetime] = None
) -> Tuple[datetime, datetime]:
    """UTC bounds of "today" in the campaign's send-window timezone —
    the day the daily throttle counts against."""
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(window.timezone)
    local = now_utc.astimezone(tz)
    day_start = datetime(local.year, local.month, local.day, tzinfo=tz)
    return day_start.astimezone(timezone.utc), (day_start + timedelta(days=1)).astimezone(timezone.utc)


def compose_footer(template: OutreachTemplate) -> str:
    """CAN-SPAM footer appended to every outreach send: identify the
    sender + business, include the physical address, offer opt-out."""
    return (
        "\n\n--\n"
        "{name} · {business}\n"
        "{address}\n"
        "If you'd rather not hear from me, just reply \"unsubscribe\" "
        "and I won't email you again."
    ).format(
        name=template.sender_name,
        business=template.sender_business,
        address=template.physical_address,
    )


def render_placeholders(text: str, values: dict) -> str:
    """Substitute ``{business_name}``-style placeholders, leaving unknown
    braces untouched (the text goes to an LLM next, not str.format)."""
    out = text
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val) if val is not None else "")
    return out
