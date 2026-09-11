"""The seam between the RAG pipeline and whoever runs the models (ADR-09).

The handler never imports a provider directly. It asks for `get_provider()` and gets
something that can `embed` and `generate`, which is what makes swapping OpenRouter for
Bedrock an environment variable rather than a refactor.

Each implementation translates its own failures into `ProviderError` with one of the kinds
below. That is the whole point of the type: the API contract promises specific error codes
(`model_throttled`, `model_unavailable`, ...) and those must not depend on whether the
underlying failure arrived as an HTTP 429 or a boto3 `ThrottlingException`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ErrorKind(str, Enum):
    """Provider-independent failure categories, mapped to HTTP by the handler."""

    THROTTLED = "throttled"
    TIMEOUT = "timeout"
    ACCESS_DENIED = "access_denied"
    BAD_REQUEST = "bad_request"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


class ProviderError(RuntimeError):
    def __init__(self, kind: ErrorKind, message: str, hint: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        # A hint is provider-specific by nature: "check the secret" and "enable model access
        # in the Bedrock console" are different actions. A generic message here costs the
        # reader half an hour.
        self.hint = hint


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


class ModelProvider(Protocol):
    """What the query path needs from a model provider. Nothing more."""

    name: str
    generation_model: str
    embedding_model: str

    def embed(self, text: str) -> list[float]:
        """Return a unit-length vector for `text`."""
        ...

    def generate(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        model: str | None = None,
    ) -> GenerationResult:
        """Answer `user` under the constraints in `system`.

        `model` overrides the configured generation model for this one call. The
        verification step wants a stronger (or simply different) model than the one that
        wrote the answer, and it should get it through the same client, retries and error
        translation rather than a second provider instance. None means the configured
        default, so every existing caller is unaffected.
        """
        ...


def normalize(vector: list[float]) -> list[float]:
    """Scale to unit length so a dot product is a cosine similarity.

    Applied to every vector regardless of what the provider claims to return. The retriever
    depends on unit vectors (ADR-02), and a provider that quietly stopped normalizing would
    produce subtly wrong scores with nothing crashing.
    """
    norm = sum(component * component for component in vector) ** 0.5
    if norm == 0.0:
        return list(vector)
    return [component / norm for component in vector]
