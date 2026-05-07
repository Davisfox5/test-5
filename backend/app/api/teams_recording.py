"""Microsoft Teams compliance recording — HTTP entry points.

Two endpoints:

* ``POST /teams/notification`` — receives Microsoft Graph change
  notifications. Handles the validation handshake (echo
  ``validationToken`` as plain text) and parses notification batches
  into :class:`ChangeNotification` objects. **Does not** persist
  ``TeamsCallRecord`` rows yet — that requires the media bot, which
  isn't deployed. We log structured events instead so the user can
  see in production logs that Graph is reaching us.
* ``POST /teams/bot/callback`` — placeholder for callbacks from the
  (future) .NET media bot. Today this returns 503 with a clear
  "media bot not deployed" message; the route exists so the URL is
  registered and infrastructure (TLS, ingress, IP allowlists) can be
  validated end-to-end before the bot ships.

This router is mounted under the standard ``/api/v1`` prefix in
``main.py``. Graph notifications must be reachable from the public
internet (Microsoft IPs); CORS is irrelevant for this surface.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.app.services.teams_recording import get_media_bot
from backend.app.services.teams_recording.bot_interface import MediaBotNotDeployedError
from backend.app.services.teams_recording.subscriptions import (
    SubscriptionValidationError,
    is_validation_handshake,
    parse_notifications,
    validation_response_body,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Graph change-notification webhook ────────────────────────────────


@router.post("/teams/notification", include_in_schema=True)
async def teams_notification(request: Request) -> Any:
    """Microsoft Graph change-notification receiver.

    Behaviour:

    * Validation handshake — when Graph creates a subscription it sends
      a one-shot ``POST`` with ``?validationToken=<random>``. We must
      reply 200 with the token echoed as ``text/plain`` within 10 s, or
      Graph refuses to register the subscription. Detected by
      :func:`is_validation_handshake`.
    * Notification batch — Graph posts ``{"value": [...]}`` for resource
      change events. We parse, log, and return 202. Persistence is
      deferred to the media-bot follow-on workstream because there is
      no useful state to write without the bot's correlated artifacts.

    No authentication is performed on this endpoint. Graph notifications
    are validated by the per-subscription ``clientState`` (rotated per
    tenant) — that's the documented Microsoft-recommended approach. In
    a follow-on we'll also pin Microsoft's IP ranges at the ingress.
    """

    # Convert query params to a plain dict for the helper. ``request.query_params``
    # is a Multi*Dict; we only care about ``validationToken`` (single-valued).
    query = {key: value for key, value in request.query_params.items()}

    if is_validation_handshake(query):
        try:
            token = validation_response_body(query)
        except SubscriptionValidationError as exc:
            logger.warning(
                "teams_recording.notification.validation_bad_request",
                extra={"error": str(exc)},
            )
            return PlainTextResponse(
                str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        logger.info("teams_recording.notification.validation_ok")
        # Microsoft's docs require text/plain with the token verbatim.
        return PlainTextResponse(token, status_code=status.HTTP_200_OK)

    # Notification batch path. Body is JSON.
    try:
        body: Dict[str, Any] = await request.json()
    except Exception as exc:  # noqa: BLE001 — Graph could send anything; be defensive
        logger.warning(
            "teams_recording.notification.bad_json",
            extra={"error": repr(exc)},
        )
        return JSONResponse(
            {"error": "request body is not valid JSON"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        # We don't know the per-subscription clientState in the scaffold
        # because there's no persistence layer for it yet. Pass None so
        # parse_notifications validates structure but not the secret. The
        # follow-on will pass the value loaded from the subscription row.
        notifications = parse_notifications(body, expected_client_state=None)
    except SubscriptionValidationError as exc:
        logger.warning(
            "teams_recording.notification.parse_failed",
            extra={"error": str(exc)},
        )
        return JSONResponse(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    logger.info(
        "teams_recording.notification.received",
        extra={
            "count": len(notifications),
            "resources": sorted({n.resource for n in notifications}),
        },
    )

    # Per Microsoft's spec, return 202 Accepted within 3 seconds. We do
    # zero downstream work here in the scaffold so the SLA is trivial.
    return JSONResponse(
        {"accepted": len(notifications)},
        status_code=status.HTTP_202_ACCEPTED,
    )


# ── .NET media bot callback (placeholder) ────────────────────────────


@router.post("/teams/bot/callback", include_in_schema=True)
async def teams_bot_callback(request: Request) -> Any:
    """Callback surface for the (future) .NET media bot.

    Today this endpoint is a placeholder. It exists so:

    * The URL can be registered with the bot at deploy time without a
      code change to the API.
    * Customer-side network validation (TLS, ingress IP ranges, mTLS
      if required) can be performed ahead of the bot rollout.

    It always returns 503 with a clear "media bot not deployed"
    message. When the .NET bot ships, this handler is replaced with the
    real lifecycle logic.
    """

    bot = get_media_bot()
    status_struct = bot.status()
    if not status_struct.deployed:
        logger.info(
            "teams_recording.bot_callback.not_deployed",
            extra={"reason": status_struct.reason},
        )
        return JSONResponse(
            {
                "deployed": False,
                "reason": status_struct.reason,
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Reachable when a real MediaBot has been registered. The real
    # contract is owned by the .NET bridge module; for now, accept the
    # callback and log it.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    try:
        bot.is_available()
    except MediaBotNotDeployedError as exc:
        return JSONResponse(
            {"deployed": False, "reason": str(exc)},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    logger.info("teams_recording.bot_callback.received", extra={"body": body})
    return JSONResponse({"received": True}, status_code=status.HTTP_200_OK)
