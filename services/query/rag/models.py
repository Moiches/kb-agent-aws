"""Data shapes for the query path.

`Chunk` mirrors the record written by the ingest Lambda into `kb-index.json.gz`. The two
services share that JSON schema, not code -- the index artifact is the contract between
them, which keeps each Lambda independently deployable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chunk:
    """One indexed passage plus its embedding."""

    chunk_id: str
    document_id: str
    text: str
    embedding: list[float]
    document_title: str | None = None
    section: str | None = None
    page: int | None = None

    @classmethod
    def from_record(cls, record: dict) -> "Chunk":
        return cls(
            chunk_id=record["chunk_id"],
            document_id=record["document_id"],
            text=record["text"],
            embedding=record["embedding"],
            document_title=record.get("document_title"),
            section=record.get("section"),
            page=record.get("page"),
        )


@dataclass(frozen=True)
class Hit:
    """A chunk retrieved for a question, with its similarity score."""

    chunk: Chunk
    score: float


@dataclass
class Source:
    """A retrieved passage as it is returned to the client."""

    document_id: str
    chunk_id: str
    score: float
    excerpt: str
    document_title: str | None = None
    section: str | None = None
    page: int | None = None
    cited: bool = False

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "document_title": self.document_title,
            "chunk_id": self.chunk_id,
            "section": self.section,
            "page": self.page,
            "score": round(self.score, 4),
            "excerpt": self.excerpt,
            "cited": self.cited,
        }


@dataclass
class QueryRequest:
    """A validated `POST /query` body."""

    question: str
    session_id: str
    top_k: int
    # "standard" or "simple". Controls register only -- both styles carry identical
    # grounding rules, so citation verification and the confidence score work the same way.
    style: str = "standard"


@dataclass
class KnowledgeBase:
    """The loaded index artifact."""

    chunks: list[Chunk] = field(default_factory=list)
    kb_version: str = "unknown"
    embedding_model: str = "unknown"
    dimensions: int = 0
    document_count: int = 0

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @classmethod
    def from_artifact(cls, payload: dict) -> "KnowledgeBase":
        return cls(
            chunks=[Chunk.from_record(r) for r in payload.get("chunks", [])],
            kb_version=payload.get("kb_version", "unknown"),
            embedding_model=payload.get("embedding_model", "unknown"),
            dimensions=int(payload.get("dimensions", 0)),
            document_count=int(payload.get("document_count", 0)),
        )


class ApiError(Exception):
    """An error that maps cleanly onto an HTTP response.

    Raising this instead of returning error dicts keeps the happy path in `handler.py`
    linear and guarantees every failure leaves through the same formatter.
    """

    def __init__(self, status: int, error: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.hint = hint
