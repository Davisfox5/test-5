"""Voyage AI embedding client.

Thin async wrapper around Voyage's embeddings endpoint. Batches inputs up to
Voyage's documented per-request limits.
"""

from __future__ import annotations

import logging
from typing import List, Literal, Sequence

import httpx

from backend.app.config import get_settings

logger = logging.getLogger(__name__)

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_BATCH_SIZE = 128  # Voyage caps at 128 inputs per request for voyage-3


class VoyageEmbedderError(RuntimeError):
    """Raised when Voyage returns an error or the request fails."""


class VoyageEmbedder:
    """Async client for Voyage embeddings."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.VOYAGE_API_KEY
        self._model = settings.VOYAGE_EMBED_MODEL
        self._dim = settings.VOYAGE_EMBED_DIM

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(
        self,
        inputs: Sequence[str],
        input_type: Literal["document", "query"] = "document",
    ) -> List[List[float]]:
        """Embed one or more strings.

        Args:
            inputs: Raw text strings.
            input_type: "document" for ingestion, "query" for retrieval.
                Voyage uses slightly different encodings for each.
        """
        if not inputs:
            return []
        if not self._api_key:
            raise VoyageEmbedderError("VOYAGE_API_KEY is not configured")

        results: List[List[float]] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(0, len(inputs), _BATCH_SIZE):
                batch = list(inputs[i : i + _BATCH_SIZE])
                payload = {
                    "model": self._model,
                    "input": batch,
                    "input_type": input_type,
                }
                headers = {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                }
                try:
                    resp = await client.post(_VOYAGE_URL, json=payload, headers=headers)
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    raise VoyageEmbedderError(f"Voyage request failed: {exc}") from exc

                data = resp.json()
                batch_embeddings = [item["embedding"] for item in data.get("data", [])]
                if len(batch_embeddings) != len(batch):
                    raise VoyageEmbedderError(
                        f"Voyage returned {len(batch_embeddings)} embeddings for "
                        f"{len(batch)} inputs"
                    )
                results.extend(batch_embeddings)

        return results
