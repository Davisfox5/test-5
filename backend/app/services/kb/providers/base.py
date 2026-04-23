"""KB provider protocol.

A provider knows how to list + fetch documents from an external
knowledge source (Drive, SharePoint, Confluence, etc.) and yield them
in a neutral ``ExternalDocument`` shape. The sync runner persists the
docs via ``ingest_document`` which handles chunking, embedding, and
index writes.

Auth tokens come from the tenant's ``Integration`` row; providers never
store credentials themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Dict, Optional, Protocol, runtime_checkable


class KBProviderError(RuntimeError):
    """Base class for KB provider failures."""


class KBProviderAuthError(KBProviderError):
    """Token invalid / refresh failed. The sync service pauses + surfaces re-auth."""


@dataclass
class ExternalDocument:
    """Normalized KB document from an external source.

    ``external_id`` is the provider-scoped id (Drive file id, Graph
    driveItem id, Confluence page id). Combined with ``source_type`` it
    uniquely identifies the doc within the tenant so repeat syncs
    upsert rather than duplicate.
    """

    external_id: str
    title: str
    content: str
    source_url: Optional[str] = None
    source_type: str = ""  # "gdrive" | "onedrive" | "sharepoint" | "confluence" | …
    updated_at: Optional[datetime] = None
    tags: list = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class KBProvider(Protocol):
    """Contract every KB provider implements."""

    source_type: str

    async def iter_documents(self) -> AsyncIterator[ExternalDocument]:
        """Yield documents from the provider in an order that suits the
        provider (Drive's modifiedTime desc, Graph delta tokens, Confluence
        page order, etc.)."""
        ...

    async def close(self) -> None:
        """Release any HTTP clients / cached tokens."""
        ...
