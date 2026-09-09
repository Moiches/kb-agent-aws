"""Citation verification and the confidence formula."""

import pytest
from rag.confidence import (
    compute_confidence,
    extract_citations,
    grounding_label,
    verify_citations,
)

FLOOR, CEIL = 0.30, 0.75

RETRIEVED = [
    "refund-and-cancellation-policy.md#chunk-2",
    "refund-and-cancellation-policy.md#chunk-3",
    "enterprise-sla.md#chunk-4",
]


# ------------------------------------------------------------------ citation handling


def test_extract_citations_finds_every_bracketed_chunk_id():
    answer = (
        "Enterprise refunds run 30 days [refund-and-cancellation-policy.md#chunk-2] "
        "and require approval [refund-and-cancellation-policy.md#chunk-3]."
    )
    assert extract_citations(answer) == RETRIEVED[:2]


def test_extract_citations_deduplicates_and_preserves_order():
    answer = "[b.md#chunk-1] then [a.md#chunk-0] then [b.md#chunk-1] again"
    assert extract_citations(answer) == ["b.md#chunk-1", "a.md#chunk-0"]


def test_verify_citations_keeps_real_ones():
    answer = "Refunds run 30 days [refund-and-cancellation-policy.md#chunk-2]."
    cleaned, kept, dropped = verify_citations(answer, set(RETRIEVED))
    assert kept == ["refund-and-cancellation-policy.md#chunk-2"]
    assert dropped == []
    assert cleaned == answer


def test_verify_citations_strips_hallucinated_ones():
    """A plausible but invented chunk_id is an unfalsifiable citation: remove it."""
    answer = (
        "Refunds run 30 days [refund-and-cancellation-policy.md#chunk-2] "
        "and are automatic [invented-policy.md#chunk-9]."
    )
    cleaned, kept, dropped = verify_citations(answer, set(RETRIEVED))

    assert kept == ["refund-and-cancellation-policy.md#chunk-2"]
    assert dropped == ["invented-policy.md#chunk-9"]
    assert "invented-policy" not in cleaned
    assert "refund-and-cancellation-policy.md#chunk-2" in cleaned


def test_verify_citations_tidies_whitespace_left_by_a_removal():
    cleaned, _, _ = verify_citations("The window is 30 days [fake.md#chunk-1] .", set())
    assert "  " not in cleaned
    assert cleaned.endswith("days.")


# ---------------------------------------------------------------------- the confidence


def test_confidence_is_always_within_bounds():
    cases = [
        ([0.95, 0.90, 0.88], RETRIEVED, RETRIEVED),
        ([0.31], [], RETRIEVED),
        ([1.0, 1.0, 1.0], RETRIEVED, RETRIEVED),
        ([0.0], [], []),
    ]
    for scores, cited, retrieved in cases:
        value, _ = compute_confidence(scores, cited, retrieved, floor=FLOOR, ceil=CEIL)
        assert 0.0 <= value <= 1.0


def test_confidence_rises_with_retrieval_strength():
    weak, _ = compute_confidence([0.35, 0.34], RETRIEVED, RETRIEVED, floor=FLOOR, ceil=CEIL)
    strong, _ = compute_confidence([0.90, 0.88], RETRIEVED, RETRIEVED, floor=FLOOR, ceil=CEIL)
    assert strong > weak


def test_unused_context_lowers_confidence():
    """Same retrieval quality, but the model cited nothing we gave it."""
    grounded, _ = compute_confidence([0.9, 0.85], RETRIEVED, RETRIEVED, floor=FLOOR, ceil=CEIL)
    ungrounded, _ = compute_confidence([0.9, 0.85], [], RETRIEVED, floor=FLOOR, ceil=CEIL)
    assert grounded > ungrounded


def test_an_isolated_top_hit_scores_below_a_supported_one():
    """Consensus term: a lone spike is less trustworthy than agreeing neighbours."""
    spike, _ = compute_confidence([0.90, 0.10, 0.05], RETRIEVED, RETRIEVED, floor=FLOOR, ceil=CEIL)
    agreement, _ = compute_confidence([0.90, 0.87, 0.85], RETRIEVED, RETRIEVED, floor=FLOOR, ceil=CEIL)
    assert agreement > spike


def test_components_are_reported_for_auditability():
    _, components = compute_confidence([0.9, 0.8], RETRIEVED, RETRIEVED, floor=FLOOR, ceil=CEIL)
    assert set(components) == {"retrieval_strength", "consensus", "citation_coverage"}
    assert all(0.0 <= v <= 1.0 for v in components.values())


def test_no_scores_means_zero_confidence():
    value, components = compute_confidence([], [], [], floor=FLOOR, ceil=CEIL)
    assert value == 0.0
    assert components["retrieval_strength"] == 0.0


def test_citations_outside_the_retrieved_set_do_not_inflate_coverage():
    _, components = compute_confidence(
        [0.9, 0.8], ["ghost.md#chunk-1"], RETRIEVED, floor=FLOOR, ceil=CEIL
    )
    assert components["citation_coverage"] == 0.0


# -------------------------------------------------------------------- grounding label


@pytest.mark.parametrize(
    "confidence,expected",
    [(0.95, "high"), (0.70, "high"), (0.69, "medium"), (0.45, "medium"), (0.44, "low"), (0.0, "low")],
)
def test_grounding_bands(confidence, expected):
    assert grounding_label(confidence, top_score=0.9, floor=FLOOR, abstained=False) == expected


def test_score_below_the_floor_is_insufficient_context_regardless_of_confidence():
    assert (
        grounding_label(0.99, top_score=0.10, floor=FLOOR, abstained=False)
        == "insufficient_context"
    )


def test_model_abstention_overrides_the_bands():
    assert (
        grounding_label(0.88, top_score=0.95, floor=FLOOR, abstained=True)
        == "insufficient_context"
    )


# ------------------------------------------------- citation coverage is top_k-independent


def test_citing_precisely_is_not_punished():
    """Regression test for a defect the evaluation set exposed.

    Coverage used to divide by the number of retrieved passages, so a model that answered
    from the single passage that mattered scored 0.2 while a wordier answer citing four
    scored 0.8 -- with worse retrieval. Precision was being penalised.
    """
    retrieved = [f"doc.md#chunk-{i}" for i in range(5)]
    _, precise = compute_confidence([0.9] * 5, retrieved[:2], retrieved, floor=FLOOR, ceil=CEIL)
    _, verbose = compute_confidence([0.9] * 5, retrieved, retrieved, floor=FLOOR, ceil=CEIL)
    assert precise["citation_coverage"] == verbose["citation_coverage"] == 1.0


def test_coverage_does_not_depend_on_top_k():
    """The same answer must score the same whether the caller asked for 3 passages or 10."""
    cited = ["doc.md#chunk-0", "doc.md#chunk-1"]
    _, small = compute_confidence(
        [0.9] * 3, cited, [f"doc.md#chunk-{i}" for i in range(3)], floor=FLOOR, ceil=CEIL
    )
    _, large = compute_confidence(
        [0.9] * 10, cited, [f"doc.md#chunk-{i}" for i in range(10)], floor=FLOOR, ceil=CEIL
    )
    assert small["citation_coverage"] == large["citation_coverage"]


def test_citing_nothing_still_scores_zero():
    """The failure the term exists to catch must still be caught."""
    retrieved = ["doc.md#chunk-0", "doc.md#chunk-1"]
    _, components = compute_confidence([0.9, 0.8], [], retrieved, floor=FLOOR, ceil=CEIL)
    assert components["citation_coverage"] == 0.0


def test_a_single_citation_earns_partial_credit():
    retrieved = ["doc.md#chunk-0", "doc.md#chunk-1", "doc.md#chunk-2"]
    _, components = compute_confidence([0.9] * 3, retrieved[:1], retrieved, floor=FLOOR, ceil=CEIL)
    assert components["citation_coverage"] == 0.5
