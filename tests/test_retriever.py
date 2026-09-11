"""Retrieval ordering, source diversity, the length-scaled cap, and degenerate inputs."""

from rag import config
from rag.models import Chunk
from rag.retriever import VectorStore, document_allowance


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


# ------------------------------------------------------------------ length-scaled cap
# A flat cap of 3 was calibrated on eight short documents and hid the one passage that could
# answer once a 174-chunk paper joined the corpus (EVALUATION.md, failure B). The allowance
# now scales with the document: max(3, ceil(RATIO * chunks)), never above top_k. Every test
# below pins the ratio explicitly so the assertions do not depend on the environment the
# suite happens to run in; the last one checks the config seam the handler relies on.
RATIO = 0.05


def long_document(document_id: str, size: int, lead: float = 1.0) -> list[Chunk]:
    """`size` chunks of one document, every one of them scoring just under `lead`.

    Chunk `i` scores `lead - i * 1e-4` against QUERY, so the document ranks chunk-0 first
    and sits, as a block, above any chunk built with a smaller `lead`.
    """
    return [
        chunk(f"{document_id}#chunk-{i}", document_id, [lead - i * 1e-4, 0.0])
        for i in range(size)
    ]


def filler(document_id: str, lead: float) -> Chunk:
    """A one-chunk document that scores exactly `lead`."""
    return chunk(f"{document_id}#chunk-0", document_id, [lead, 0.0])


def test_a_174_chunk_document_gets_nine_slots_at_top_k_ten():
    """ceil(0.05 * 174) = 9: the paper takes nine of ten slots and one is left for the rest."""
    store = VectorStore(long_document("paper.pdf", 174) + [filler("short.md", 0.5)])
    hits = store.search(QUERY, top_k=10, max_per_document=3, per_document_ratio=RATIO)
    documents = [h.chunk.document_id for h in hits]

    assert documents.count("paper.pdf") == 9
    assert documents.count("short.md") == 1


def test_the_allowance_never_exceeds_top_k():
    """At top_k=5 the same paper may fill every slot: an allowance of nine is bounded by five."""
    store = VectorStore(long_document("paper.pdf", 174) + [filler("short.md", 0.5)])
    hits = store.search(QUERY, top_k=5, max_per_document=3, per_document_ratio=RATIO)

    assert [h.chunk.document_id for h in hits] == ["paper.pdf"] * 5


def test_a_short_document_keeps_the_flat_cap():
    """ceil(0.05 * 10) = 1, so the floor of 3 wins: short documents behave exactly as before."""
    store = VectorStore(long_document("a.md", 10) + [filler("b.md", 0.5), filler("c.md", 0.4)])
    hits = store.search(QUERY, top_k=10, max_per_document=3, per_document_ratio=RATIO)
    documents = [h.chunk.document_id for h in hits]

    assert documents.count("a.md") == 3
    assert documents[3] == "b.md", "the fourth slot goes to the next document, not a fourth a.md"


def test_two_long_documents_still_share_the_context():
    """Scaling the cap must not reintroduce the monopoly the cap exists to prevent.

    Two 100-chunk documents each get ceil(0.05 * 100) = 5. At top_k=6 the higher-scoring one
    takes five slots and the sixth still reaches the second document.
    """
    store = VectorStore(long_document("a.pdf", 100, lead=1.0) + long_document("b.pdf", 100, lead=0.9))
    hits = store.search(QUERY, top_k=6, max_per_document=3, per_document_ratio=RATIO)
    documents = [h.chunk.document_id for h in hits]

    assert documents.count("a.pdf") == 5
    assert documents.count("b.pdf") == 1


def test_allowance_is_exactly_three_up_to_sixty_chunks():
    """The identity that keeps the evaluation corpus untouched: ceil(0.05 * 60) is 3, not 4.

    Binary 0.05 is slightly above one twentieth, so this guards the boundary against float
    drift as much as it documents the rule.
    """
    assert all(document_allowance(n, top_k=10, floor=3, ratio=RATIO) == 3 for n in range(1, 61))
    assert document_allowance(61, top_k=10, floor=3, ratio=RATIO) == 4


def test_scaled_cap_is_a_no_op_on_documents_of_sixty_chunks_or_fewer():
    """Byte-identical results below the boundary.

    A ratio of zero is the old flat rule. The default must agree with it, hit for hit, on a
    corpus whose largest document sits exactly at the 60-chunk boundary -- and the evaluation
    corpus is 52 chunks in total, so nothing in it comes close.
    """
    store = VectorStore(
        long_document("boundary.md", 60)
        + long_document("mid.md", 12, lead=0.9)
        + [filler("short.md", 0.5)]
    )
    for top_k in (1, 3, 5, 10):
        scaled = store.search(QUERY, top_k=top_k, max_per_document=3, per_document_ratio=RATIO)
        flat = store.search(QUERY, top_k=top_k, max_per_document=3, per_document_ratio=0.0)
        assert scaled == flat, f"top_k={top_k}"


def test_the_ratio_is_read_from_config_when_not_passed(monkeypatch):
    """The handler passes only `max_per_document`; the ratio has to reach it through config."""
    store = VectorStore(long_document("paper.pdf", 174) + [filler("short.md", 0.5)])

    monkeypatch.setattr(config, "MAX_CHUNKS_PER_DOC_RATIO", 0.05)
    hits = store.search(QUERY, top_k=10, max_per_document=3)
    assert [h.chunk.document_id for h in hits].count("paper.pdf") == 9

    monkeypatch.setattr(config, "MAX_CHUNKS_PER_DOC_RATIO", 0.0)
    hits = store.search(QUERY, top_k=10, max_per_document=3)
    assert [h.chunk.document_id for h in hits].count("paper.pdf") == 3
