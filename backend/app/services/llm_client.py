"""Shared Anthropic async client — single factory so services don't re-init per request."""

from __future__ import annotations

from functools import lru_cache

import anthropic

from backend.app.config import get_settings


@lru_cache(maxsize=1)
def get_async_anthropic() -> anthropic.AsyncAnthropic:
    """Return a process-wide AsyncAnthropic client."""
    settings = get_settings()
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
