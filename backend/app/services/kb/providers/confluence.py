"""Confluence provider — Cloud + Data Center / Server.

Walks pages under a given space (or all spaces the token can see),
strips the XHTML storage format down to readable text, and yields
one document per page.

Auth modes:

* **API token** (Cloud) — HTTP Basic with ``(email, api_token)``.
* **Personal Access Token** (Data Center / Server) — Bearer header.

The ``auth_mode`` discriminator lives on the Integration row's
``provider_config`` so the same tenant can switch between Cloud and
self-hosted without re-plumbing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from backend.app.services.kb.providers.base import (
    ExternalDocument,
    KBProviderAuthError,
    KBProviderError,
)

logger = logging.getLogger(__name__)


class ConfluenceProvider:
    source_type = "confluence"

    def __init__(
        self,
        *,
        base_url: str,
        auth_mode: str = "basic",  # "basic" (Cloud) | "bearer" (DC/Server)
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        personal_access_token: Optional[str] = None,
        space_keys: Optional[List[str]] = None,
    ) -> None:
        if not base_url:
            raise KBProviderAuthError("Confluence base_url is required")
        if auth_mode == "basic":
            if not (email and api_token):
                raise KBProviderAuthError(
                    "Confluence Cloud requires email + api_token"
                )
        elif auth_mode == "bearer":
            if not personal_access_token:
                raise KBProviderAuthError(
                    "Confluence DC/Server requires personal_access_token"
                )
        else:
            raise KBProviderAuthError(f"Unknown Confluence auth_mode: {auth_mode}")

        self._base_url = base_url.rstrip("/")
        self._auth_mode = auth_mode
        self._email = email or ""
        self._api_token = api_token or ""
        self._pat = personal_access_token or ""
        self._space_keys = [s for s in (space_keys or []) if s]
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def iter_documents(self) -> AsyncIterator[ExternalDocument]:
        if self._space_keys:
            for key in self._space_keys:
                async for doc in self._iter_space(key):
                    yield doc
        else:
            async for doc in self._iter_space(None):
                yield doc

    async def _iter_space(self, space_key: Optional[str]) -> AsyncIterator[ExternalDocument]:
        start = 0
        limit = 50
        while True:
            params: Dict[str, Any] = {
                "type": "page",
                "status": "current",
                "expand": "body.storage,version,space",
                "limit": limit,
                "start": start,
            }
            if space_key:
                params["spaceKey"] = space_key

            resp = await self._get("/rest/api/content", params=params)
            data = resp.json()
            results = data.get("results") or []
            for page in results:
                storage = (page.get("body") or {}).get("storage") or {}
                xhtml = storage.get("value") or ""
                text = _xhtml_to_text(xhtml)
                if not text.strip():
                    continue
                page_id = str(page.get("id", ""))
                title = page.get("title") or "Untitled"
                web_ui = (
                    self._base_url
                    + ((page.get("_links") or {}).get("webui") or "")
                )
                space = (page.get("space") or {}).get("key")
                version = (page.get("version") or {}).get("when")
                yield ExternalDocument(
                    external_id=page_id,
                    title=str(title),
                    content=text,
                    source_url=web_ui,
                    source_type=self.source_type,
                    updated_at=_parse_confluence_ts(version),
                    metadata={"space_key": space},
                )
            if len(results) < limit:
                break
            start += limit

    async def _get(self, path: str, *, params: Dict[str, Any]) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers: Dict[str, str] = {"Accept": "application/json"}
        auth: Optional[tuple[str, str]] = None
        if self._auth_mode == "basic":
            auth = (self._email, self._api_token)
        else:
            headers["Authorization"] = f"Bearer {self._pat}"
        resp = await self._client.get(url, params=params, headers=headers, auth=auth)
        if resp.status_code == 401:
            raise KBProviderAuthError("Confluence rejected the credentials")
        if resp.status_code >= 400:
            raise KBProviderError(
                f"Confluence {path} failed: {resp.status_code} {resp.text[:200]}"
            )
        return resp


# ── Helpers ──────────────────────────────────────────────────────────


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _xhtml_to_text(xhtml: str) -> str:
    """Crude but reliable storage-format → text conversion.

    Confluence storage is XHTML plus a sprinkling of custom
    ``<ac:*>`` / ``<ri:*>`` macros. We strip tags and collapse
    whitespace. For a small perf cost we could parse with lxml and
    render macros (code blocks, panels) better, but plain-text keeps
    the embedding pipeline predictable.
    """
    stripped = _TAG_RE.sub(" ", xhtml or "")
    stripped = stripped.replace("&nbsp;", " ").replace("&amp;", "&")
    return _WS_RE.sub(" ", stripped).strip()


def _parse_confluence_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
