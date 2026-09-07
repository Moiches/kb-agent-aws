"""In-memory vector retrieval.

Design note (this is the decision most worth explaining to a reviewer):

The knowledge base is a plain list of chunks held in Lambda memory, and search is an
exact cosine scan -- no FAISS, no numpy, no approximate index. Two reasons:

1.  *It is not the bottleneck.* At ~300 chunks x 512 dimensions a scan is ~150k
    multiply-adds, which CPython does in tens of milliseconds. The Bedrock generation
    call that follows takes 700-1600 ms. An ANN index would optimise 2% of the latency
    while adding a native dependency to every deployment.

2.  *It keeps the Lambda dependency-free.* `faiss-cpu` needs manylinux wheels and, from
    a Windows workstation, Docker. With a pure-Python scan the query Lambda ships with
    nothing but the runtime's own boto3, so `cdk deploy` works anywhere.

Titan Text Embeddings V2 is asked for normalized vectors, so cosine similarity is just
the dot product.

`VectorStore` is the seam where this decision can be reversed: swapping in numpy, FAISS,
pgvector or Bedrock Knowledge Bases means replacing this one class. Practical ceiling
for the current implementation is roughly 50k chunks.
"""

from __future__ import annotations

from .models import Chunk, Hit


class VectorStore:
    """Exact cosine search over an in-memory list of chunks."""

    def __init__(self, chunks: list[Chunk]):
        self._chunks = chunks

    def __len__(self) -> int:
        return len(self._chunks)

    def search(self, query_vector: list[float], top_k: int, max_per_document: int = 0) -> list[Hit]:
        """Return the `top_k` most similar chunks, highest score first.

        `max_per_document` caps how many chunks a single document may contribute. It
        diversifies sources so one long document cannot monopolise the context window,
        which matters for questions whose answer spans two documents. Zero disables it.
        """
        if not self._chunks or top_k <= 0:
            return []

        scored = sorted(
            (Hit(chunk=chunk, score=_dot(query_vector, chunk.embedding)) for chunk in self._chunks),
            key=lambda hit: hit.score,
            reverse=True,
        )

        if max_per_document <= 0:
            return scored[:top_k]

        selected: list[Hit] = []
        per_document: dict[str, int] = {}
        for hit in scored:
            used = per_document.get(hit.chunk.document_id, 0)
            if used >= max_per_document:
                continue
            per_document[hit.chunk.document_id] = used + 1
            selected.append(hit)
            if len(selected) == top_k:
                break
        return selected


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product == cosine similarity, because both vectors are unit-normalized."""
    return sum(x * y for x, y in zip(a, b))
