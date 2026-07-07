"""Tests for the Notion KB provider.

Uses an httpx MockTransport so no network is touched: one page from
``/v1/search`` whose block children flatten into a document.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.app.services.kb.providers.base import KBProviderAuthError
from backend.app.services.kb.providers.notion import NotionProvider


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/search":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "object": "page",
                        "id": "page-1",
                        "url": "https://notion.so/page-1",
                        "last_edited_time": "2026-01-02T03:04:05.000Z",
                        "properties": {
                            "Name": {
                                "type": "title",
                                "title": [{"plain_text": "Runbook"}],
                            }
                        },
                    }
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )
    if path == "/v1/blocks/page-1/children":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "heading_1",
                        "id": "b1",
                        "has_children": False,
                        "heading_1": {"rich_text": [{"plain_text": "Onboarding"}]},
                    },
                    {
                        "type": "paragraph",
                        "id": "b2",
                        "has_children": False,
                        "paragraph": {
                            "rich_text": [{"plain_text": "Step one. Step two."}]
                        },
                    },
                    # A non-text block with no rich_text is skipped cleanly.
                    {"type": "divider", "id": "b3", "has_children": False, "divider": {}},
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )
    return httpx.Response(404, json={"message": "not found"})


@pytest.mark.asyncio
async def test_notion_iter_documents():
    provider = NotionProvider(access_token="secret_abc")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    docs = [d async for d in provider.iter_documents()]
    await provider.close()

    assert len(docs) == 1
    doc = docs[0]
    assert doc.external_id == "page-1"
    assert doc.title == "Runbook"
    assert doc.source_type == "notion"
    assert doc.source_url == "https://notion.so/page-1"
    assert "Onboarding" in doc.content
    assert "Step one. Step two." in doc.content
    assert doc.updated_at is not None


@pytest.mark.asyncio
async def test_notion_auth_error_on_401():
    def unauth(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    provider = NotionProvider(access_token="bad")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(unauth))
    with pytest.raises(KBProviderAuthError):
        [d async for d in provider.iter_documents()]
    await provider.close()


def test_notion_requires_token():
    with pytest.raises(KBProviderAuthError):
        NotionProvider(access_token="")
