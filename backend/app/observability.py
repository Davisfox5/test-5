"""Error monitoring + metrics plumbing.

Two surfaces:

1. :func:`init_sentry` — wires Sentry's SDK into FastAPI, Celery,
   SQLAlchemy, and Redis in one call. Called from ``main.py`` (for
   the web side) and ``tasks.py`` (for the worker side). Missing
   ``SENTRY_DSN`` is a no-op, so local dev doesn't ship fake events.
2. :class:`SentryPIIScrubber` — a ``before_send`` hook that strips
   obvious PII (emails, phone numbers, API keys) from breadcrumbs and
   exception values before they leave the process. Belt-and-braces on
   top of Sentry's own default scrubbers.

This module deliberately doesn't import Sentry at the top level —
``sentry-sdk`` is in requirements.txt but we import lazily so the
app still boots when it's missing (e.g. CI without the optional dep
installed).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

from backend.app.config import get_settings
from backend.app.logging_setup import current_context

logger = logging.getLogger(__name__)


# ── PII scrubbing ───────────────────────────────────────────────────

# Coarse patterns — deliberately over-redact rather than under-redact.
# Presidio runs on the transcript path already; Sentry only sees the
# occasional stray string that slipped into a log line, so these catch
# the common shapes.
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{7,}\d")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]{12,}", re.I)
_APIKEY_RE = re.compile(r"sk-[A-Za-z0-9]{16,}")


def _scrub(value: Any) -> Any:
    """Recursively redact PII-shaped strings inside dicts/lists."""
    if isinstance(value, str):
        v = value
        v = _EMAIL_RE.sub("[email]", v)
        v = _PHONE_RE.sub("[phone]", v)
        v = _BEARER_RE.sub("Bearer [token]", v)
        v = _APIKEY_RE.sub("sk-[redacted]", v)
        return v
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _before_send(event: Dict[str, Any], hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Scrub PII + attach LINDA context vars as tags.

    ``tenant_id`` / ``request_id`` / ``interaction_id`` become Sentry
    tags so you can filter the issue list by tenant without grepping
    the message body.
    """
    context = current_context()
    if context:
        tags = event.setdefault("tags", {})
        for key, value in context.items():
            tags.setdefault(key, value)
    try:
        event["message"] = _scrub(event.get("message"))
        event["breadcrumbs"] = _scrub(event.get("breadcrumbs"))
        exception = event.get("exception") or {}
        for ex in exception.get("values", []) or []:
            ex["value"] = _scrub(ex.get("value"))
    except Exception:
        logger.debug("sentry before_send scrub raised", exc_info=True)
    return event


# ── Init ────────────────────────────────────────────────────────────

_initialized = False


def init_sentry() -> bool:
    """Start Sentry if configured. Safe to call twice.

    Returns True when Sentry is active, False when it's not. The
    caller can use the return value to gate features that require
    Sentry (e.g. shipping breadcrumbs from specific paths).
    """
    global _initialized
    if _initialized:
        return True
    settings = get_settings()
    dsn = getattr(settings, "SENTRY_DSN", None) or os.environ.get("SENTRY_DSN")
    if not dsn:
        logger.info("SENTRY_DSN not set; error monitoring disabled")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.redis import RedisIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except Exception:
        logger.warning(
            "sentry-sdk not installed — errors will not be reported. "
            "Install with: pip install 'sentry-sdk[fastapi,celery]'"
        )
        return False

    environment = getattr(settings, "ENVIRONMENT", None) or os.environ.get(
        "ENVIRONMENT", "local"
    )
    release = getattr(settings, "RELEASE_VERSION", None) or os.environ.get(
        "RELEASE_VERSION"
    )
    traces_rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
    profiles_rate = float(os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0.0"))

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release,
            traces_sample_rate=traces_rate,
            profiles_sample_rate=profiles_rate,
            send_default_pii=False,
            before_send=_before_send,
            integrations=[
                FastApiIntegration(),
                CeleryIntegration(),
                SqlalchemyIntegration(),
                RedisIntegration(),
                LoggingIntegration(
                    level=logging.INFO,
                    event_level=logging.ERROR,
                ),
            ],
        )
    except Exception:
        logger.exception("Sentry init failed")
        return False

    _initialized = True
    logger.info("Sentry error monitoring active (env=%s)", environment)
    return True


__all__ = ["init_sentry"]
