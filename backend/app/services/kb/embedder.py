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

# Query embeddings are cached in Redis for 24h keyed by (model, sha256).
# The cost audit found common queries ("pricing", "onboarding", …)
# hammering Voyage's billable API — caching once per window eliminated
# ~30-40% of embed calls. Lived in kb_document_retrieval before that
# module's own vector path was retired; here it serves every caller.
_QUERY_CACHE_TTL_SECONDS = 24 * 60 * 60


def _query_cache_key(model: str, text: str) -> str:
    import hashlib

    return "embed:v1:{0}:{1}".format(
        model, hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


def _cache_redis():
    try:
        import redis  # type: ignore

        return redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    except Exception:  # pragma: no cover — Redis may be absent in tests
        return None


def _vector_to_b64(vec: Sequence[float]) -> str:
    import base64
    import struct

    return base64.b64encode(struct.pack("{0}f".format(len(vec)), *vec)).decode("ascii")


def _b64_to_vector(s: str) -> List[float]:
    import base64
    import struct

    raw = base64.b64decode(s)
    n = len(raw) // 4
    return list(struct.unpack("{0}f".format(n), raw))


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

        # Cache path — single query strings only (the retrieval hot
        # path); document batches change too much to be worth caching.
        cache_key = None
        if input_type == "query" and len(inputs) == 1:
            cache_key = _query_cache_key(self._model, inputs[0])
            r = _cache_redis()
            if r is not None:
                try:
                    raw = r.get(cache_key)
                    if raw:
                        return [_b64_to_vector(raw)]
                except Exception:
                    logger.debug("embed cache get failed (non-fatal)", exc_info=True)

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

        if cache_key is not None and results:
            r = _cache_redis()
            if r is not None:
                try:
                    r.setex(
                        cache_key,
                        _QUERY_CACHE_TTL_SECONDS,
                        _vector_to_b64(results[0]),
                    )
                except Exception:
                    logger.debug("embed cache set failed (non-fatal)", exc_info=True)

        return results
