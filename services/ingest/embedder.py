"""Embedding client for the ingestion pipeline.

Talks to OpenRouter's OpenAI-compatible `/v1/embeddings` endpoint using `urllib` from the
standard library. No `requests`: the deployment package stays at one third-party dependency
(`pypdf`, pure Python), which is what lets this Lambda be built with `pip install -t` on any
OS without Docker.

Two behaviours here are deliberate and worth reading before changing:

*   **Vectors are always normalized, whatever the provider returns.** Measured on
    2026-09-07, `text-embedding-3-small` at 512 dimensions comes back with an L2 norm of
    1.000238 -- normalized to float32 precision. But OpenRouter is a *router*: the same slug
    can be served by a different upstream tomorrow and nothing in the contract promises unit
    vectors. The retriever treats cosine similarity as a plain dot product (ADR-02), so if
    that assumption ever broke, every score would be quietly wrong with nothing crashing.
    Two lines here make it impossible.

*   **Retries are bounded and jittered.** Rate limits are per account, and the OpenRouter
    credit may be shared, so a burst of ingestion requests is exactly the situation that
    provokes a 429.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/text-embedding-3-small"
DEFAULT_DIMENSIONS = 512

# The endpoint accepts a list of inputs. Batching cuts a 52-chunk corpus from 52 round trips
# to two, which matters when each one costs ~600 ms.
DEFAULT_BATCH_SIZE = 32

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class EmbeddingError(RuntimeError):
    """Raised when embeddings cannot be produced after retrying."""


class OpenRouterEmbedder:
    """Turns text into unit-length vectors."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        base_url: str = DEFAULT_BASE_URL,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        if not api_key:
            raise EmbeddingError("no API key supplied")
        self._api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._batch_size = max(1, batch_size)

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed many texts, preserving input order."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(self._embed_one_batch(batch))
        return vectors

    def _embed_one_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch, "dimensions": self.dimensions}
        body = self._post("/embeddings", payload)

        rows = body.get("data")
        if not rows or len(rows) != len(batch):
            raise EmbeddingError(
                f"expected {len(batch)} embeddings, got {len(rows or [])}: {json.dumps(body)[:200]}"
            )

        # The API documents an `index` field; sorting by it rather than trusting array order
        # costs nothing and removes a class of silent misalignment where every chunk would
        # carry someone else's vector.
        rows = sorted(rows, key=lambda row: row.get("index", 0))
        return [normalize(row["embedding"]) for row in rows]

    def _post(self, path: str, payload: dict) -> dict:
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            request = urllib.request.Request(
                f"{self._base_url}{path}",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "kb-agent-aws ingest",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code not in RETRYABLE_STATUS:
                    raise EmbeddingError(last_error) from None
            except urllib.error.URLError as exc:
                last_error = f"network error: {exc.reason}"

            if attempt < MAX_ATTEMPTS:
                # Full jitter: several Lambdas retrying in lockstep is how a rate limit turns
                # into a thundering herd.
                delay = random.uniform(0, BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                time.sleep(delay)

        raise EmbeddingError(f"giving up after {MAX_ATTEMPTS} attempts. Last error: {last_error}")


def normalize(vector: list[float]) -> list[float]:
    """Scale a vector to unit length so that a dot product is a cosine similarity.

    A zero vector is returned unchanged rather than producing a division by zero; it can
    only arise from a provider fault, and the caller sees it as a chunk that matches nothing
    instead of a crashed ingestion run.
    """
    norm = sum(component * component for component in vector) ** 0.5
    if norm == 0.0:
        return list(vector)
    return [component / norm for component in vector]
