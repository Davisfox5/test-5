"""Microsoft Graph change-notification subscription manager.

Two pieces of Graph state matter for compliance recording:

* ``/communications/callRecords`` — the post-call analytics resource
  (CDR-style records produced after a Teams call ends). Useful for
  reconciling against the live capture and for non-realtime
  use-cases.
* ``/communications/onlineMeetings/getAllRecordings`` — change
  notifications for recordings produced by Teams' built-in meeting
  recording (a different surface from the compliance-recording
  media bot). Customers without the media bot can still ingest these.

Each requires a registered ``subscription`` with Graph. Subscriptions
expire — at most ~3 days for ``callRecords`` per Microsoft's docs — so
something has to renew them. This module:

1. Builds the subscription create/renew request bodies.
2. Verifies the validation handshake Graph sends to the notification
   endpoint at registration time (``validationToken`` query param echoed
   back as plain text).
3. Parses inbound change notifications and validates the per-tenant
   ``clientState`` matches what we registered with.

The actual HTTP call to Graph is wrapped behind a small async helper
that takes an ``httpx.AsyncClient`` (or a stub in tests) — we don't
hard-code an httpx import at module scope so the test path can patch
without network. Tests pass synthetic Graph payloads through the
parsing helpers; the network side is deliberately not exercised in
this scaffold round.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.app.services.teams_recording.graph_app_auth import (
    GraphAppAuth,
    GraphAppAuthError,
    get_graph_app_auth,
)

logger = logging.getLogger(__name__)


# Graph cap on subscription lifetime for the resources we care about.
# The ``callRecords`` resource caps at about 4230 minutes (~3 days);
# online-meeting recordings cap at 60 minutes. We pick a conservative
# 50-minute window so the renewal job has slack on both. Per Microsoft:
# https://learn.microsoft.com/graph/api/resources/subscription
_DEFAULT_LIFETIME_MIN = 50

# Resources we know how to subscribe to. Adding more requires Microsoft
# permissions review — keep this list tight and explicit.
SUPPORTED_RESOURCES = (
    "communications/callRecords",
    "communications/onlineMeetings/getAllRecordings",
)


class SubscriptionValidationError(ValueError):
    """Raised when a Graph notification fails validation — bad
    ``clientState``, malformed envelope, or unrecognised resource.
    API handlers convert this to a 400."""


@dataclass
class SubscriptionSpec:
    """Inputs needed to create a Graph change-notification subscription.

    ``resource`` is one of :data:`SUPPORTED_RESOURCES`. ``notification_url``
    must be HTTPS in production (Graph rejects plain HTTP) and reachable
    from Microsoft's network. ``client_state`` is a per-subscription
    secret we generate and persist; Graph echoes it on every inbound
    notification so we can detect tampered or replayed payloads.
    """

    resource: str
    notification_url: str
    client_state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    change_type: str = "created,updated"
    lifetime_minutes: int = _DEFAULT_LIFETIME_MIN

    def to_graph_body(self) -> Dict[str, Any]:
        """Render the JSON body for ``POST /v1.0/subscriptions``."""

        if self.resource not in SUPPORTED_RESOURCES:
            raise SubscriptionValidationError(
                f"resource {self.resource!r} is not in SUPPORTED_RESOURCES"
            )
        if not self.notification_url.startswith("https://"):
            raise SubscriptionValidationError(
                "Graph requires HTTPS notification_url"
            )
        expires = datetime.now(timezone.utc) + timedelta(
            minutes=self.lifetime_minutes
        )
        return {
            "changeType": self.change_type,
            "notificationUrl": self.notification_url,
            "resource": self.resource,
            # Graph wants ISO-8601 with millisecond precision and Z suffix.
            "expirationDateTime": expires.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "clientState": self.client_state,
        }


@dataclass
class ChangeNotification:
    """One change notification entry as received from Graph.

    Graph posts batches under ``{"value": [ ... ]}``; this dataclass is
    a per-entry projection. ``raw`` carries the entire dict for any
    handler that needs fields we haven't surfaced.
    """

    subscription_id: str
    client_state: Optional[str]
    change_type: str
    resource: str
    resource_data_id: Optional[str]
    tenant_id: Optional[str]
    raw: Dict[str, Any]


# ── Validation handshake ──────────────────────────────────────────────


def is_validation_handshake(query_params: Dict[str, str]) -> bool:
    """Graph sends a one-shot validation request with
    ``?validationToken=<random>`` when a subscription is first created.

    The endpoint must respond 200 with the token echoed back as
    ``text/plain`` within 10 seconds — anything else and Graph refuses
    to register the subscription. This helper detects the case so the
    API handler can short-circuit before any auth or DB work.
    """

    return "validationToken" in query_params


def validation_response_body(query_params: Dict[str, str]) -> str:
    """Return the body to echo for a validation handshake.

    Raises :class:`SubscriptionValidationError` if the query is missing
    or empty, which the handler converts to a 400.
    """

    token = query_params.get("validationToken", "")
    if not token:
        raise SubscriptionValidationError(
            "validation handshake requires a non-empty validationToken"
        )
    return token


# ── Notification parsing ─────────────────────────────────────────────


def parse_notifications(
    payload: Dict[str, Any],
    *,
    expected_client_state: Optional[str] = None,
) -> List[ChangeNotification]:
    """Parse a Graph change-notification batch into our internal shape.

    Graph posts ``{"value": [ {...}, ... ]}``. Each entry has at minimum
    ``subscriptionId``, ``changeType``, ``resource``, and ``clientState``.
    When ``expected_client_state`` is provided, every entry must match —
    otherwise we raise. (Multi-tenant deployments will eventually pass
    a callback here to look up the expected state per ``subscriptionId``;
    for the scaffold a single shared secret is sufficient.)
    """

    if not isinstance(payload, dict):
        raise SubscriptionValidationError("notification payload is not a JSON object")
    raw_entries = payload.get("value")
    if not isinstance(raw_entries, list):
        raise SubscriptionValidationError(
            "notification payload missing 'value' array"
        )

    parsed: List[ChangeNotification] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise SubscriptionValidationError(
                "notification entry is not a JSON object"
            )
        subscription_id = entry.get("subscriptionId")
        client_state = entry.get("clientState")
        change_type = entry.get("changeType")
        resource = entry.get("resource")
        if not (subscription_id and change_type and resource):
            raise SubscriptionValidationError(
                "notification entry missing subscriptionId/changeType/resource"
            )
        if expected_client_state is not None and client_state != expected_client_state:
            raise SubscriptionValidationError(
                "notification clientState does not match expected value"
            )
        resource_data = entry.get("resourceData") or {}
        parsed.append(
            ChangeNotification(
                subscription_id=subscription_id,
                client_state=client_state,
                change_type=change_type,
                resource=resource,
                resource_data_id=resource_data.get("id"),
                tenant_id=entry.get("tenantId"),
                raw=entry,
            )
        )
    return parsed


# ── Subscription lifecycle stubs ─────────────────────────────────────


@dataclass
class CreateSubscriptionResult:
    subscription_id: str
    expiration: datetime
    client_state: str
    raw: Dict[str, Any]


async def create_subscription(
    spec: SubscriptionSpec,
    *,
    auth: Optional[GraphAppAuth] = None,
    http_client: Any = None,
) -> CreateSubscriptionResult:
    """Send ``POST /v1.0/subscriptions`` to Graph.

    Returns the subscription id Graph assigned plus the expiration so
    the caller can persist both for the renewal scheduler.

    This function is *intentionally not exercised in the scaffold's CI*
    — we don't want to make a real Graph call from tests, and the value
    of mocking the wire format here is low until the surrounding
    persistence layer is built. The implementation is here so the
    follow-on workstream can wire it without re-deriving the URL, body,
    auth header, or retry semantics.
    """

    auth = auth or get_graph_app_auth()
    body = spec.to_graph_body()  # raises if invalid

    if http_client is None:  # pragma: no cover - exercised at integration time
        import httpx

        http_client = httpx.AsyncClient(timeout=30.0)
        owns_client = True
    else:
        owns_client = False

    try:
        try:
            header = auth.authorization_header()
        except GraphAppAuthError as exc:
            logger.error("teams_recording.subscription.no_auth", exc_info=exc)
            raise

        response = await http_client.post(
            "https://graph.microsoft.com/v1.0/subscriptions",
            json=body,
            headers={
                "Authorization": header,
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise SubscriptionValidationError(
                f"Graph rejected subscription create: {response.status_code} "
                f"{getattr(response, 'text', '')[:500]}"
            )
        data = response.json()
    finally:
        if owns_client:
            await http_client.aclose()

    expiration = _parse_iso8601(data.get("expirationDateTime"))
    return CreateSubscriptionResult(
        subscription_id=data["id"],
        expiration=expiration,
        client_state=spec.client_state,
        raw=data,
    )


def _parse_iso8601(raw: Optional[str]) -> datetime:
    """Best-effort parse of Graph's ISO-8601 timestamp.

    Graph emits ``2026-05-07T15:00:00.0000000Z`` style strings; Python's
    ``fromisoformat`` (3.11+) handles these natively, but on 3.9 we have
    to massage the format. Accept a missing/malformed value by returning
    epoch — the caller persists it for diagnostics, not for correctness.
    """

    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    cleaned = raw.replace("Z", "+00:00")
    # Trim sub-second precision past microseconds (Graph emits 7 digits).
    if "." in cleaned:
        head, tail = cleaned.split(".", 1)
        if "+" in tail:
            frac, tz = tail.split("+", 1)
            cleaned = f"{head}.{frac[:6]}+{tz}"
        else:
            cleaned = f"{head}.{tail[:6]}"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
