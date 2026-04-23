"""SignalWire adapter.

SignalWire's LaML (Markup Language) is a compatible clone of Twilio's
TwiML, and their REST API mirrors Twilio's shape one-for-one, just
hosted on ``{space}.signalwire.com``. So we reuse the Twilio TwiML
builders verbatim and only implement what genuinely differs:

* REST base URL — built from ``Integration.provider_config["space_url"]``.
* Auth — Basic with ``(project_id, api_token)`` where Twilio uses
  ``(account_sid, auth_token)``.
* Webhook signature — SignalWire's compatibility API emits the same
  HMAC-SHA1 signature Twilio does, so ``validate_twilio_signature``
  applies directly.

We deliberately don't re-export TwiML builders from this module — the
Twilio ones work as-is; duplicating them would just create drift.
"""

from __future__ import annotations

from typing import Optional


def signalwire_rest_base(space_url: str) -> str:
    """Turn ``{space}.signalwire.com`` into the REST API base URL.

    Accepts either a bare domain or a full ``https://…`` prefix.
    """
    if not space_url:
        raise ValueError("SignalWire space_url is required (e.g. acme.signalwire.com)")
    root = space_url.strip()
    if not root.startswith("http"):
        root = "https://" + root
    root = root.rstrip("/")
    return f"{root}/api/laml/2010-04-01"


def build_calls_url(space_url: str, project_id: str) -> str:
    """POST this URL to place an outbound call (compat with Twilio)."""
    if not project_id:
        raise ValueError("SignalWire project_id is required")
    base = signalwire_rest_base(space_url)
    return f"{base}/Accounts/{project_id}/Calls.json"


def build_update_call_url(space_url: str, project_id: str, call_sid: str) -> str:
    """POST this URL to modify an in-flight call (redirect TwiML, etc.)."""
    if not call_sid:
        raise ValueError("call_sid is required")
    base = signalwire_rest_base(space_url)
    return f"{base}/Accounts/{project_id}/Calls/{call_sid}.json"
