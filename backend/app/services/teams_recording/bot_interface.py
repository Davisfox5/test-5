"""Abstract media-bot interface + stub implementation.

Microsoft's Teams compliance recording certification requires that the
*media plane* — the component that joins a Teams call as a hidden
compliance recorder, mixes the participants' audio, and dispatches frames
elsewhere — runs a stateful .NET media bot built against the Microsoft
Graph Communications Calling SDK. That stack does not have a Python
equivalent; implementing it is its own multi-month workstream and is
out of scope for this round.

This module defines the seam where the .NET bot will eventually plug in,
and ships a stub implementation that always reports "not deployed". With
the stub in place, every other layer (subscription manager, webhook
handlers, model rows) can be built and tested today against synthetic
fixtures, and the only swap when the bot lands is registering a real
``MediaBot`` instance via ``set_media_bot_factory``.

The interface is intentionally narrow. The real bot will run as a
separate process — likely a Windows Service or Azure Container App
written in C#. It will call back into LINDA's API via
``POST /teams/bot/callback`` (in ``api/teams_recording.py``) when calls
attach/detach. The Python control plane never speaks the media plane
directly. So this interface only needs:

* ``is_available()`` — fast health check the API uses to say
  "media plane is reachable" vs "scaffold-only".
* ``attach_to_call(call_id)`` — request that the bot join the named
  call. Returns a correlation id that downstream callbacks reference.
* ``detach(call_id)`` — request graceful detach.
* ``status()`` — diagnostic struct for the admin UI.

All four methods are sync and tiny in scope; the heavy lifting is on
the .NET side.
"""

from __future__ import annotations

import abc
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class MediaBotStatus:
    """Snapshot of the media bot's reachability + version.

    ``deployed`` is the only field a routing decision should branch
    on — everything else is for the admin diagnostic UI.
    """

    deployed: bool
    reason: str
    bot_version: Optional[str] = None
    last_heartbeat_at: Optional[float] = None


class MediaBot(abc.ABC):
    """The contract the (future) .NET media bot adapter implements.

    There is at most one ``MediaBot`` per process. The factory pattern
    in ``get_media_bot`` lets the .NET bridge replace the stub at app
    startup without a runtime branch in every caller.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def status(self) -> MediaBotStatus:
        """Cheap, sync. Used by health checks and the admin dashboard."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether the bot can currently accept attach requests."""

    @abc.abstractmethod
    def attach_to_call(self, call_id: str) -> str:
        """Ask the bot to join ``call_id``.

        Returns a correlation id that will appear on the bot's later
        callbacks for this call. The control plane uses this id to
        reconcile lifecycle messages back to its own ``TeamsCallRecord``
        rows. Implementations must not block on the bot's actual join
        — that's an async, multi-second process. This call should
        return as soon as the join request has been *enqueued* with the
        bot.
        """

    @abc.abstractmethod
    def detach(self, call_id: str) -> None:
        """Ask the bot to leave ``call_id`` cleanly. No-op if the bot
        isn't currently in that call."""


class StubMediaBot(MediaBot):
    """Default media bot used everywhere until the .NET bot is shipped.

    Every method either reports "not deployed" or no-ops. Callers must
    treat ``is_available() is False`` as the steady-state truth: any
    code path that requires the media plane should refuse the request
    with a clear "media bot not deployed" error rather than masking
    it as a soft failure. That refusal is what makes the scaffold
    safe to land in production — we won't accidentally silently drop
    Teams call audio, because we never claim to capture it.
    """

    name = "stub"

    REASON = (
        "Microsoft Teams Compliance Recording media bot is not deployed. "
        "The .NET stateful media bot, Azure infrastructure, and Microsoft "
        "certification submission are tracked in "
        "docs/integrations/stream-3-teams/USER_TODO.md."
    )

    def status(self) -> MediaBotStatus:
        logger.debug("teams_recording.stub_bot.status_checked")
        return MediaBotStatus(deployed=False, reason=self.REASON)

    def is_available(self) -> bool:
        return False

    def attach_to_call(self, call_id: str) -> str:
        logger.warning(
            "teams_recording.stub_bot.attach_refused",
            extra={"call_id": call_id},
        )
        raise MediaBotNotDeployedError(self.REASON)

    def detach(self, call_id: str) -> None:
        logger.debug(
            "teams_recording.stub_bot.detach_noop",
            extra={"call_id": call_id},
        )


class MediaBotNotDeployedError(RuntimeError):
    """Raised by the stub bot when callers ask it to do work that
    requires a real media plane. API handlers convert this to a 503."""


# ── Process-wide registration ─────────────────────────────────────────

_lock = threading.Lock()
_factory: Optional[Callable[[], MediaBot]] = None
_instance: Optional[MediaBot] = None


def set_media_bot_factory(factory: Callable[[], MediaBot]) -> None:
    """Install a custom factory. Used by the (future) .NET bridge at
    app startup, and by tests that want to assert the wiring."""

    global _factory, _instance
    with _lock:
        _factory = factory
        _instance = None


def get_media_bot() -> MediaBot:
    """Return the process-wide ``MediaBot`` instance.

    Defaults to ``StubMediaBot`` when nothing has been registered. Lazy
    instantiation means the stub is constructed exactly once per
    process, even when the factory is the default."""

    global _instance
    with _lock:
        if _instance is None:
            factory = _factory or StubMediaBot
            _instance = factory()
        return _instance


def reset_for_tests() -> None:
    """Drop the cached factory + instance. Test-only."""

    global _factory, _instance
    with _lock:
        _factory = None
        _instance = None
