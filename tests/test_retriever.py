"""Retrieval ordering, source diversity, and degenerate inputs."""

from rag.models import Chunk
from rag.retriever import VectorStore


def chunk(chunk_id: str, document_id: str, vector: list[float]) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_id,
        text=f"text of {chunk_id}",
        embedding=vector,
    )


# Unit vectors on a 2D circle make the expected ranking obvious by inspection.
QUERY = [1.0, 0.0]


def build_store() -> VectorStore:
    return VectorStore(
        [
            chunk("a.md#chunk-0", "a.md", [1.00, 0.00]),   # score 1.00
            chunk("a.md#chunk-1", "a.md", [0.98, 0.20]),   # score 0.98
            chunk("a.md#chunk-2", "a.md", [0.95, 0.31]),   # score 0.95
            chunk("a.md#chunk-3", "a.md", [0.92, 0.39]),   # score 0.92
            chunk("b.md#chunk-0", "b.md", [0.80, 0.60]),   # score 0.80
            chunk("c.md#chunk-0", "c.md", [0.00, 1.00]),   # score 0.00
        ]
    )


def test_results_are_ordered_by_descending_score():
    hits = build_store().search(QUERY, top_k=6)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0].chunk.chunk_id == "a.md#chunk-0"


def test_top_k_limits_the_result_count():
    assert len(build_store().search(QUERY, top_k=3)) == 3


def test_top_k_larger_than_the_index_returns_everything():
    assert len(build_store().search(QUERY, top_k=99)) == 6


def test_max_per_document_diversifies_sources():
    """Without a cap, a.md would take all four top slots and b.md would never surface."""
    hits = build_store().search(QUERY, top_k=5, max_per_document=2)
    documents = [h.chunk.document_id for h in hits]

    assert documents.count("a.md") == 2
    assert "b.md" in documents, "a second document should get a slot"


def test_max_per_document_zero_disables_the_cap():
    hits = build_store().search(QUERY, top_k=4, max_per_document=0)
    assert [h.chunk.document_id for h in hits] == ["a.md"] * 4


def test_empty_index_returns_no_hits():
    assert VectorStore([]).search(QUERY, top_k=5) == []


def test_non_positive_top_k_returns_no_hits():
    assert build_store().search(QUERY, top_k=0) == []


def test_scores_are_the_dot_product():
    hits = build_store().search(QUERY, top_k=1)
    assert abs(hits[0].score - 1.0) < 1e-9
