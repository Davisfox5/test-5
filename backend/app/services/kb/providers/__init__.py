"""KB document-source providers.

Each provider iterates documents from an external knowledge source and
yields :class:`ExternalDocument` shapes that the sync runner feeds
through the existing ``ingest_document`` pipeline (chunk → embed →
store).

Providers implemented here:

* :mod:`gdrive` — Google Drive + Google Docs (Workspace & Enterprise).
* :mod:`onedrive` — OneDrive + SharePoint via Microsoft Graph.
* :mod:`confluence` — Atlassian Confluence Cloud + Server REST API.

Third-party direct-ingest paths (generic API push, MCP servers) don't
need adapters — they call ``ingest_document`` directly from the API
routes that accept them.
"""

from backend.app.services.kb.providers.base import (
    ExternalDocument,
    KBProvider,
    KBProviderAuthError,
    KBProviderError,
)

__all__ = [
    "ExternalDocument",
    "KBProvider",
    "KBProviderAuthError",
    "KBProviderError",
]
