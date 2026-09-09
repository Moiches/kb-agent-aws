"""Model provider selection (ADR-09).

`MODEL_PROVIDER` decides which implementation the handler gets. OpenRouter is the default
and the only path exercised end to end, because the AMCRO sandbox has no Bedrock access and
the solution has to be replicable there.

A Bedrock implementation is deliberately absent rather than written-and-untestable. The
interface is what makes the switch cheap; a second implementation nobody can run would be
unverified code shipped in a deliverable, which is worse than an honest gap. Adding it is
one class against this Protocol, and `scripts/check_openrouter.py` shows the shape of the
verification it would need.
"""

from __future__ import annotations

import functools

from .base import ErrorKind, GenerationResult, ModelProvider, ProviderError, normalize
from .openrouter import OpenRouterProvider

__all__ = [
    "ErrorKind",
    "GenerationResult",
    "ModelProvider",
    "ProviderError",
    "get_provider",
    "normalize",
]

_SUPPORTED = ("openrouter",)


@functools.lru_cache(maxsize=1)
def _cached(provider: str, api_key: str, generation_model: str, embedding_model: str, dimensions: int):
    if provider == "openrouter":
        return OpenRouterProvider(
            api_key=api_key,
            generation_model=generation_model,
            embedding_model=embedding_model,
            embedding_dimensions=dimensions,
        )
    raise ProviderError(
        ErrorKind.INTERNAL,
        f"unknown MODEL_PROVIDER {provider!r}; supported: {', '.join(_SUPPORTED)}",
    )


def get_provider(
    provider: str,
    api_key: str,
    generation_model: str,
    embedding_model: str,
    dimensions: int,
) -> ModelProvider:
    """Build (or reuse) the configured provider.

    Cached across invocations of a warm Lambda, so the second request onwards pays nothing
    for construction.
    """
    return _cached(provider, api_key, generation_model, embedding_model, dimensions)
