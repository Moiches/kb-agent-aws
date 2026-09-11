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
pgvector or Bedrock Knowledge Bases means replacing this one class.

The practical ceiling is **~13,300 chunks**, and it is the ingest Lambda's 600 s timeout at a
measured 45 ms/chunk -- not this scan, and not memory. Memory runs out next, at ~19k chunks,
because `index_store.load()` holds the old index live while parsing the new one. An earlier
version of this docstring claimed ~50k; that was measured and found false (50k peaks at
1,150-1,460 MiB against a 1,024 MB allocation). See ADR-11.
"""

from __future__ import annotations

import math
from collections import Counter

from . import config
from .models import Chunk, Hit


class VectorStore:
    """Exact cosine search over an in-memory list of chunks."""

    def __init__(self, chunks: list[Chunk]):
        self._chunks = chunks
        # Chunk count per document, for the length-scaled cap in `search`. One O(n) pass at
        # construction, next to the O(n x dims) scan every search does anyway.
        self._doc_sizes = Counter(chunk.document_id for chunk in chunks)

    def __len__(self) -> int:
        return len(self._chunks)

    def search(
        self,
        query_vector: list[float],
        top_k: int,
        max_per_document: int = 0,
        per_document_ratio: float | None = None,
    ) -> list[Hit]:
        """Return the `top_k` most similar chunks, highest score first.

        `max_per_document` caps how many chunks a single document may contribute. It
        diversifies sources so one long document cannot monopolise the context window,
        which matters for questions whose answer spans two documents. Zero disables it.

        The cap is a floor, not a constant: a document of `n` chunks may contribute
        `max(max_per_document, ceil(per_document_ratio * n))` hits, never more than `top_k`
        (see `document_allowance`). A flat cap of 3 was calibrated on eight short documents
        and failed silently when a 174-chunk paper joined the corpus -- three passages were
        under 2% of the only document that could answer, and raising `top_k` could not help
        because the cap, not `top_k`, was binding (EVALUATION.md, failure B). The ratio
        defaults to `config.MAX_CHUNKS_PER_DOC_RATIO`, so callers that only know about the
        flat cap get the scaled rule; pass it explicitly to pin a value in tests.
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

        ratio = config.MAX_CHUNKS_PER_DOC_RATIO if per_document_ratio is None else per_document_ratio
        allowance = {
            document_id: document_allowance(size, top_k, max_per_document, ratio)
            for document_id, size in self._doc_sizes.items()
        }

        selected: list[Hit] = []
        per_document: dict[str, int] = {}
        for hit in scored:
            document_id = hit.chunk.document_id
            used = per_document.get(document_id, 0)
            if used >= allowance[document_id]:
                continue
            per_document[document_id] = used + 1
            selected.append(hit)
            if len(selected) == top_k:
                break
        return selected


def document_allowance(chunk_count: int, top_k: int, floor: int, ratio: float) -> int:
    """How many hits a document of `chunk_count` chunks may contribute to one search.

    `max(floor, ceil(ratio * chunk_count))`, bounded by `top_k` because no document can
    contribute more than the whole result. With the defaults (floor 3, ratio 0.05) the
    allowance is exactly 3 for any document of 60 chunks or fewer -- ceil(0.05 * 60) is 3,
    and the float product does not drift above it -- which is what keeps retrieval on the
    current 52-chunk corpus byte-identical to the flat cap this replaces. A 174-chunk paper
    gets 9 of 10 slots at top_k 10 and all 5 at top_k 5. A ratio of zero is the flat cap.
    """
    return min(top_k, max(floor, math.ceil(ratio * chunk_count)))


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product == cosine similarity, because both vectors are unit-normalized."""
    return sum(x * y for x, y in zip(a, b))
