"""Structured-logging setup.

Ships three things worth knowing about:

1. **JSON formatter.** Every log record becomes one JSON line —
   ``timestamp``, ``level``, ``logger``, ``message``, and any context
   vars the request/task propagated. That shape is what every log
   aggregator (Datadog, CloudWatch Insights, Loki, Humio) expects.
2. **Context vars.** ``request_id``, ``tenant_id``, ``interaction_id``,
   ``user_id``, ``session_id`` live in :class:`contextvars.ContextVar`
   so they're threadsafe + asyncio-safe. Set once at the edge (HTTP
   middleware, WS handler, Celery task) and every log line the request
   fans out to carries them automatically.
3. **Exception serialization.** Stack traces get flattened into a
   single JSON field so a log search for an error doesn't need to
   stitch multiple lines back together.

Opt-in via ``LOG_FORMAT=json`` in the environment. Default stays
human-readable so local dev doesn't get spammy.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ── Context vars — set once at the edge, read everywhere ────────────

request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[Optional[str]] = ContextVar("tenant_id", default=None)
interaction_id_var: ContextVar[Optional[str]] = ContextVar(
    "interaction_id", default=None
)
user_id_var: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
session_id_var: ContextVar[Optional[str]] = ContextVar("session_id", default=None)


_CONTEXT_VARS: dict[str, ContextVar] = {
    "request_id": request_id_var,
    "tenant_id": tenant_id_var,
    "interaction_id": interaction_id_var,
    "user_id": user_id_var,
    "session_id": session_id_var,
}


def bind_context(**values: Optional[str]) -> list[Any]:
    """Set context vars and return opaque reset tokens.

    Usage::

        tokens = bind_context(request_id=x, tenant_id=y)
        try:
            ...
        finally:
            reset_context(tokens)

    The reset_context helper is what makes this safe against scope
    bleed in Celery workers that reuse threads.
    """
    tokens: list[Any] = []
    for key, value in values.items():
        if key not in _CONTEXT_VARS:
            continue
        tokens.append(_CONTEXT_VARS[key].set(value))
    return tokens


def reset_context(tokens: Iterable[Any]) -> None:
    # ContextVar.reset takes the token the original .set() returned.
    # We store them in list order and match by ContextVar identity.
    for token in tokens:
        try:
            token.var.reset(token)
        except Exception:
            # Defensive: a test that captured tokens from a different
            # thread shouldn't crash the happy path.
            continue


def current_context() -> dict[str, str]:
    """Snapshot of all context vars that are currently populated —
    useful for ad-hoc logging or for attaching to error reports.
    """
    snapshot: dict[str, str] = {}
    for key, var in _CONTEXT_VARS.items():
        value = var.get()
        if value:
            snapshot[key] = value
    return snapshot


# ── JSON formatter ──────────────────────────────────────────────────


class JsonFormatter(logging.Formatter):
    """One JSON object per record. Fields:

    * Always: ``ts``, ``level``, ``logger``, ``msg``.
    * Conditionally: ``request_id``, ``tenant_id``, ``interaction_id``,
      ``user_id``, ``session_id`` — only when the corresponding
      context var is set.
    * When logging an exception: ``exc_type``, ``exc_msg``, ``exc_trace``.
    * ``extra={…}`` keys flow through verbatim, so call sites can
      attach e.g. ``logger.info("billed", extra={"cents": 420})``.
    """

    # Fields on LogRecord that already have a home in the output or
    # that would pollute the payload if we round-tripped them.
    _RESERVED = frozenset(
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "message",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(current_context())

        if record.exc_info:
            exc_type, exc_value, _tb = record.exc_info
            payload["exc_type"] = getattr(exc_type, "__name__", str(exc_type))
            payload["exc_msg"] = str(exc_value)
            payload["exc_trace"] = self.formatException(record.exc_info)

        # Any ``extra={…}`` keys not already claimed.
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key in payload:
                continue
            if key.startswith("_"):
                continue
            try:
                json.dumps(value)  # ensure serializable
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)

        return json.dumps(payload, default=str)


# ── Entry point ─────────────────────────────────────────────────────


def configure_logging() -> None:
    """Install handlers + formatter. Idempotent — safe to call twice.

    Controlled by two env vars:
    * ``LOG_FORMAT`` = ``json`` | ``text`` (default ``text``)
    * ``LOG_LEVEL`` = ``DEBUG`` | ``INFO`` | ``WARNING`` | … (default ``INFO``)
    """
    root = logging.getLogger()
    # Remove existing handlers so re-calling doesn't double up.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.environ.get("LOG_FORMAT", "text").lower()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s — %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root.addHandler(handler)
    root.setLevel(level)

    # Tone down libraries that are noisy at INFO.
    for noisy in (
        "httpx",
        "httpcore",
        "urllib3",
        "botocore",
        "boto3",
        "s3transfer",
        "aiormq",
    ):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def new_request_id() -> str:
    """Cheap UUID4 for correlation — wraps ``uuid.uuid4`` so the call
    site doesn't need to import it."""
    return str(uuid.uuid4())


__all__ = [
    "configure_logging",
    "bind_context",
    "reset_context",
    "current_context",
    "new_request_id",
    "JsonFormatter",
    "request_id_var",
    "tenant_id_var",
    "interaction_id_var",
    "user_id_var",
    "session_id_var",
]
