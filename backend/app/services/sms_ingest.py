"""SMS ingestion — STUBBED.

SMS and WhatsApp are intentionally disabled at this stage. They were
confounding email/voice sentiment analysis during tenant onboarding.

To re-enable:
  1. Restore "sms"/"whatsapp" to the Literal in backend/app/api/interactions.py
  2. Wire the Twilio inbound webhook route into backend/app/main.py
  3. Replace the placeholders below with real Twilio SDK calls
  4. Re-add the SMS tab to website/demo.html and CHANNEL_ICONS in demo.js

The Twilio SDK is still declared in requirements.txt so the module
imports cleanly, but nothing calls into it.
"""

from __future__ import annotations

from typing import Any, Dict


class SMSIngestionDisabled(RuntimeError):
    """Raised when something tries to ingest SMS while the channel is off."""


def handle_inbound_sms(payload: Dict[str, Any]) -> None:
    """Placeholder for Twilio inbound webhook.

    Kept as a function signature so the webhook route can be reinstated
    without hunting for where the handler used to live.
    """
    raise SMSIngestionDisabled(
        "SMS ingestion is disabled. See services/sms_ingest.py for re-enable steps."
    )


def send_outbound_sms(to: str, body: str, tenant_id: str) -> None:
    """Placeholder for Twilio outbound send."""
    raise SMSIngestionDisabled(
        "SMS sending is disabled. See services/sms_ingest.py for re-enable steps."
    )
