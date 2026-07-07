"""Notion provider — Notion API v1 (OAuth internal/public integration).

Enumerates every page the connected integration can see via the
``/v1/search`` endpoint, then walks each page's block tree and flattens
the text-bearing blocks into a single readable document.

Auth is a Bearer access token — either a public-integration OAuth token
(minted through ``api/oauth.py``'s ``notion`` provider) or an internal
integration secret pasted by the tenant. Either way it lands in the
Integration row's ``access_token`` and is handed to this provider by the
sync runner; the provider never stores credentials itself.

Notion access tokens don't expire and there is no refresh grant, so
unlike the Google/Microsoft providers there is no token-refresh
callback here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from backend.app.services.kb.providers.base import (
    ExternalDocument,
    KBProviderAuthError,
    KBProviderError,
)

logger = logging.getLogger(__name__)

# Pin the API version — Notion requires this header and evolves the
# response shape behind it. Bump deliberately after testing.
_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"

# Block types whose ``rich_text`` we treat as document body. Notion has
# ~30 block types; these are the text-bearing ones worth embedding.
_TEXT_BLOCK_TYPES = (
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "toggle",
    "quote",
    "callout",
    "code",
)

# Guard against pathologically deep / wide page trees.
_MAX_BLOCK_DEPTH = 6


class NotionProvider:
    source_type = "notion"

    def __init__(
        self,
        *,
        access_token: str,
        page_size: int = 100,
    ) -> None:
        if not access_token:
            raise KBProviderAuthError("Notion access_token is required")
        self._token = access_token
        self._page_size = page_size
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def iter_documents(self) -> AsyncIterator[ExternalDocument]:
        start_cursor: Optional[str] = None
        while True:
            body: Dict[str, Any] = {
                "filter": {"value": "page", "property": "object"},
                "page_size": self._page_size,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor

            data = (await self._post("/search", json_body=body)).json()
            for page in data.get("results") or []:
                if page.get("object") != "page":
                    continue
                doc = await self._build_document(page)
                if doc is not None:
                    yield doc

            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")
            if not start_cursor:
                break

    async def _build_document(self, page: Dict[str, Any]) -> Optional[ExternalDocument]:
        page_id = str(page.get("id") or "")
        if not page_id:
            return None
        title = _extract_title(page.get("properties") or {}) or "Untitled"
        text = await self._collect_page_text(page_id)
        if not text.strip():
            return None
        return ExternalDocument(
            external_id=page_id,
            title=title,
            content=text,
            source_url=page.get("url"),
            source_type=self.source_type,
            updated_at=_parse_ts(page.get("last_edited_time")),
        )

    async def _collect_page_text(self, block_id: str, depth: int = 0) -> str:
        """Depth-first walk of a block subtree, flattening text blocks."""
        if depth >= _MAX_BLOCK_DEPTH:
            return ""
        parts: List[str] = []
        start_cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"page_size": self._page_size}
            if start_cursor:
                params["start_cursor"] = start_cursor
            data = (
                await self._get(f"/blocks/{block_id}/children", params=params)
            ).json()
            for block in data.get("results") or []:
                btype = block.get("type")
                if btype in _TEXT_BLOCK_TYPES:
                    line = _rich_text_to_plain((block.get(btype) or {}).get("rich_text"))
                    if line:
                        parts.append(line)
                if block.get("has_children"):
                    child_id = block.get("id")
                    if child_id:
                        child_text = await self._collect_page_text(child_id, depth + 1)
                        if child_text:
                            parts.append(child_text)
            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")
            if not start_cursor:
                break
        return "\n".join(parts)

    # ── HTTP ─────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": _NOTION_VERSION,
            "Accept": "application/json",
        }

    async def _get(self, path: str, *, params: Dict[str, Any]) -> httpx.Response:
        resp = await self._client.get(
            f"{_BASE_URL}{path}", params=params, headers=self._headers()
        )
        return self._check(resp, path)

    async def _post(self, path: str, *, json_body: Dict[str, Any]) -> httpx.Response:
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        resp = await self._client.post(
            f"{_BASE_URL}{path}", json=json_body, headers=headers
        )
        return self._check(resp, path)

    @staticmethod
    def _check(resp: httpx.Response, path: str) -> httpx.Response:
        if resp.status_code in (401, 403):
            raise KBProviderAuthError("Notion rejected the access token")
        if resp.status_code >= 400:
            raise KBProviderError(
                f"Notion {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp


# ── Helpers ──────────────────────────────────────────────────────────


def _rich_text_to_plain(rich_text: Optional[List[Dict[str, Any]]]) -> str:
    if not rich_text:
        return ""
    out = []
    for span in rich_text:
        txt = span.get("plain_text")
        if txt is None:
            txt = (span.get("text") or {}).get("content")
        if txt:
            out.append(txt)
    return "".join(out).strip()


def _extract_title(properties: Dict[str, Any]) -> str:
    """Find the page's title property (the one Notion types as ``title``)."""
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _rich_text_to_plain(prop.get("title"))
    return ""


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
