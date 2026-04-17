"""Notification service — Slack and Microsoft Teams webhook delivery."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class NotificationService:
    """Send notifications to Slack and Microsoft Teams via incoming webhooks."""

    async def notify_slack(
        self,
        webhook_url: str,
        message: str,
        blocks: Optional[List[Dict]] = None,
    ) -> None:
        """Post a message to a Slack incoming webhook.

        Args:
            webhook_url: Slack webhook URL.
            message: Fallback text (always included).
            blocks: Optional Block Kit blocks for rich formatting.
        """
        payload: Dict = {"text": message}
        if blocks is not None:
            payload["blocks"] = blocks

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(webhook_url, json=payload)
            logger.info("Slack notification sent: status=%d", response.status_code)
        except Exception:
            logger.exception("Slack notification failed: url=%s", webhook_url)

    async def notify_teams(
        self,
        webhook_url: str,
        title: str,
        text: str,
    ) -> None:
        """Post a message to a Microsoft Teams Connector webhook using an Adaptive Card.

        Args:
            webhook_url: Teams incoming webhook URL.
            title: Card title.
            text: Card body text.
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        card_payload: Dict = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {
                                "type": "TextBlock",
                                "size": "Medium",
                                "weight": "Bolder",
                                "text": title,
                            },
                            {
                                "type": "TextBlock",
                                "text": text,
                                "wrap": True,
                            },
                            {
                                "type": "TextBlock",
                                "text": f"Sent at {timestamp}",
                                "size": "Small",
                                "isSubtle": True,
                            },
                        ],
                    },
                }
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(webhook_url, json=card_payload)
            logger.info("Teams notification sent: status=%d", response.status_code)
        except Exception:
            logger.exception("Teams notification failed: url=%s", webhook_url)
