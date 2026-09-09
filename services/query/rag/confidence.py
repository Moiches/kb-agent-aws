"""Citation verification and confidence scoring.

The brief asks how uncertainty is represented. The answer has to be auditable rather
than a magic number, so it is built from three terms that are each returned to the
caller in `metadata.confidence_components`.

    confidence = 0.50 * retrieval_strength   -- did we find anything genuinely similar?
               + 0.25 * consensus            -- do several passages agree, or is the top hit a fluke?
               + 0.25 * citation_coverage    -- did the model actually use the passages we gave it?

Known limitation, stated plainly because it matters: **this measures retrieval quality,
not factual correctness.** A perfect retrieval followed by a bad generation still scores
high. Closing that gap needs a second verification pass over the answer, which would
roughly double per-query cost; it is listed as future work rather than implemented.
"""

from __future__ import annotations

import re

# Matches the citation form the system prompt asks for: [some-document.md#chunk-4]
CITATION_RE = re.compile(r"\[([A-Za-z0-9._\-/ ]+#chunk-\d+)\]")

GROUNDING_HIGH = 0.70
GROUNDING_MEDIUM = 0.45

# Citations beyond this add no evidence of grounding. See compute_confidence.
CITATION_SUFFICIENCY = 2


def extract_citations(answer: str) -> list[str]:
    """Every chunk_id the model cited, in order of first appearance."""
    seen: dict[str, None] = {}
    for match in CITATION_RE.findall(answer):
        seen.setdefault(match.strip(), None)
    return list(seen)


def verify_citations(answer: str, valid_chunk_ids: set[str]) -> tuple[str, list[str], list[str]]:
    """Strip citations that do not correspond to a retrieved passage.

    A model that invents a plausible-looking chunk_id is producing an unfalsifiable
    citation, which is worse than no citation at all. Dropping them costs about a
    millisecond and is reported in `metadata.dropped_citations` so the behaviour is
    visible rather than silent.

    Returns (cleaned_answer, kept_citations, dropped_citations).
    """
    kept: list[str] = []
    dropped: list[str] = []

    for citation in extract_citations(answer):
        (kept if citation in valid_chunk_ids else dropped).append(citation)

    cleaned = answer
    for citation in dropped:
        cleaned = cleaned.replace(f"[{citation}]", "")
    # Tidy up the whitespace and stray punctuation left behind by a removal.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)

    return cleaned.strip(), kept, dropped


def compute_confidence(
    scores: list[float],
    cited_chunk_ids: list[str],
    retrieved_chunk_ids: list[str],
    *,
    floor: float,
    ceil: float,
) -> tuple[float, dict]:
    """Combine the three signals into a score in [0, 1] plus its components."""
    if not scores:
        components = {"retrieval_strength": 0.0, "consensus": 0.0, "citation_coverage": 0.0}
        return 0.0, components

    top_score = scores[0]

    # How far above the "nothing relevant" floor is the best match?
    span = max(ceil - floor, 1e-9)
    retrieval_strength = _clamp01((top_score - floor) / span)

    # Do the runners-up support the leader, or is it an isolated spike?
    consensus = _clamp01((sum(scores) / len(scores)) / top_score) if top_score > 0 else 0.0

    # Did the model lean on the passages at all?
    #
    # This term guards against one specific failure: the model ignoring the context and
    # answering from memory. It is NOT a measure of how much of the context was used.
    #
    # The original version divided by the number of retrieved passages, which made the score
    # depend on `top_k` -- a value the caller chooses, unrelated to how many passages an
    # answer needs. Measured on the evaluation set, that inverted quality: Q2 retrieved
    # perfectly (strength 1.0) and cited the single passage that answered the question,
    # scoring 0.2; Q9, the hardest question with the weakest retrieval, cited four passages
    # and scored 0.8. Precision was being punished.
    #
    # Two citations is the bar. Beyond that, more citations are not more grounding -- they
    # are usually just a wordier answer.
    retrieved = set(retrieved_chunk_ids)
    used = len({c for c in cited_chunk_ids if c in retrieved})
    citation_coverage = _clamp01(used / CITATION_SUFFICIENCY) if retrieved else 0.0

    confidence = (
        0.50 * retrieval_strength + 0.25 * consensus + 0.25 * citation_coverage
    )

    components = {
        "retrieval_strength": round(retrieval_strength, 4),
        "consensus": round(consensus, 4),
        "citation_coverage": round(citation_coverage, 4),
    }
    return round(_clamp01(confidence), 2), components


def grounding_label(confidence: float, top_score: float, *, floor: float, abstained: bool) -> str:
    """Bucket the score into something a human can act on.

    A number like 0.63 tells a user nothing; "medium, check the sources" does.
    """
    if abstained or top_score < floor:
        return "insufficient_context"
    if confidence >= GROUNDING_HIGH:
        return "high"
    if confidence >= GROUNDING_MEDIUM:
        return "medium"
    return "low"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
