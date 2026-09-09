"""OpenRouter provider: embeddings and generation over one OpenAI-compatible API.

Uses `urllib` from the standard library rather than `requests`, which keeps the query
Lambda at zero third-party dependencies (ADR-02): no build step, no Docker, no layer, and a
deployment package small enough that cold starts stay in the hundreds of milliseconds.

Measured on 2026-09-07: ~625 ms to embed, ~950 ms for a six-token completion. Both are
materially slower than an in-region AWS call, which is the price of leaving AWS.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

from .base import ErrorKind, GenerationResult, ProviderError, normalize

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 25

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

# OpenRouter answers an unrecognised key with "Missing Authentication header", which reads
# like a client bug rather than a credential problem. Worth translating: the first time it
# appeared it cost an hour of looking in the wrong place.
_MISLEADING_AUTH_MESSAGE = "Missing Authentication header"


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        generation_model: str,
        embedding_model: str,
        embedding_dimensions: int,
        base_url: str = DEFAULT_BASE_URL,
    ):
        if not api_key:
            raise ProviderError(
                ErrorKind.ACCESS_DENIED,
                "no provider API key configured",
                hint="populate the provider API key secret, then redeploy or re-invoke",
            )
        self._api_key = api_key
        self.generation_model = generation_model
        self.embedding_model = embedding_model
        self._dimensions = embedding_dimensions
        self._base_url = base_url.rstrip("/")

    def embed(self, text: str) -> list[float]:
        body = self._post(
            "/embeddings",
            {"model": self.embedding_model, "input": [text], "dimensions": self._dimensions},
        )
        rows = body.get("data") or []
        if not rows:
            raise ProviderError(ErrorKind.INTERNAL, f"no embedding returned: {json.dumps(body)[:200]}")
        return normalize(rows[0]["embedding"])

    def generate(self, system: str, user: str, max_tokens: int, temperature: float) -> GenerationResult:
        body = self._post(
            "/chat/completions",
            {
                "model": self.generation_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError(ErrorKind.INTERNAL, f"no completion returned: {json.dumps(body)[:200]}")

        usage = body.get("usage") or {}
        return GenerationResult(
            text=(choices[0].get("message", {}).get("content") or "").strip(),
            # What actually served the request, which can differ from what was asked for:
            # OpenRouter routes, and the response is the only honest source for the log.
            model=body.get("model", self.generation_model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )

    def _post(self, path: str, payload: dict) -> dict:
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            request = urllib.request.Request(
                f"{self._base_url}{path}",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "kb-agent-aws",
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
                    raise self._translate(exc.code, detail) from None
            except TimeoutError:
                last_error = f"timed out after {REQUEST_TIMEOUT_SECONDS}s"
                raise ProviderError(ErrorKind.TIMEOUT, last_error) from None
            except urllib.error.URLError as exc:
                last_error = f"network error: {exc.reason}"

            if attempt < MAX_ATTEMPTS:
                # Full jitter. A synchronous API request is waiting on this, so the ceiling
                # is deliberately lower than the ingest path's.
                time.sleep(random.uniform(0, BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))))

        raise ProviderError(ErrorKind.UNAVAILABLE, f"provider unavailable: {last_error}")

    @staticmethod
    def _translate(status: int, detail: str) -> ProviderError:
        if status in (401, 403):
            hint = "check the provider API key secret"
            if _MISLEADING_AUTH_MESSAGE in detail:
                hint += (
                    " -- OpenRouter reports an unrecognised key as "
                    f'"{_MISLEADING_AUTH_MESSAGE}", which usually means the key is wrong '
                    "or truncated, not that the header is absent"
                )
            return ProviderError(ErrorKind.ACCESS_DENIED, f"provider rejected the credential: {detail}", hint)
        if status == 400:
            return ProviderError(ErrorKind.BAD_REQUEST, f"provider rejected the request: {detail}")
        if status == 404:
            return ProviderError(
                ErrorKind.BAD_REQUEST,
                f"model not found: {detail}",
                hint="verify the slug against openrouter.ai/models",
            )
        return ProviderError(ErrorKind.INTERNAL, f"HTTP {status}: {detail}")
