"""Chunking behaviour: size limits, overlap, front-matter handling, no lost text."""

import pytest
from loaders import Segment, load_markdown, load_text
from splitter import chunk_segments, split_text

PARAGRAPH = (
    "Enterprise customers may request a refund within 30 days of the invoice date. "
    "Requests submitted after the 30-day window are not eligible for a refund, "
    "regardless of usage level. The account manager assigned to the contract must "
    "approve every Enterprise refund before Finance processes it. "
)


def test_short_text_is_a_single_chunk():
    assert split_text("A short policy note.", chunk_size=800) == ["A short policy note."]


def test_empty_text_yields_no_chunks():
    assert split_text("   \n\n  ") == []


def test_chunks_respect_the_size_limit():
    chunks = split_text(PARAGRAPH * 12, chunk_size=400, chunk_overlap=60)
    assert len(chunks) > 1
    assert all(len(c) <= 400 for c in chunks), [len(c) for c in chunks]


def test_consecutive_chunks_overlap():
    """A sentence straddling a boundary must stay retrievable from one side or the other."""
    chunks = split_text(PARAGRAPH * 8, chunk_size=400, chunk_overlap=100)
    overlapping = 0
    for first, second in zip(chunks, chunks[1:]):
        tail_words = first.split()[-6:]
        if tail_words and " ".join(tail_words) in second:
            overlapping += 1
    assert overlapping >= 1, "expected at least one shared tail between adjacent chunks"


def test_no_source_words_are_dropped():
    text = PARAGRAPH * 6
    chunks = split_text(text, chunk_size=350, chunk_overlap=50)
    joined = " ".join(chunks)
    for word in set(text.split()):
        assert word in joined, f"word lost during splitting: {word!r}"


def test_overlap_must_be_smaller_than_chunk_size():
    with pytest.raises(ValueError):
        split_text(PARAGRAPH, chunk_size=100, chunk_overlap=100)


def test_oversized_single_token_is_hard_split():
    chunks = split_text("x" * 1000, chunk_size=300, chunk_overlap=50)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)


# --------------------------------------------------------------------------- markdown


MARKDOWN = """# Enterprise Service Level Agreement

**Document owner:** Customer Success
**Last reviewed:** 2026-02-02
**Applies to:** Enterprise plan customers only

## Uptime commitment

Northwind commits to a monthly uptime of 99.9 percent.

## Service credits

Credits are applied against future invoices and are never paid out in cash.
"""


def test_markdown_sections_become_segment_metadata():
    document = load_markdown(MARKDOWN)
    sections = [s.section for s in document.segments]
    assert sections == ["Uptime commitment", "Service credits"]


def test_h1_becomes_the_document_title_not_a_segment():
    document = load_markdown(MARKDOWN)
    assert document.title == "Enterprise Service Level Agreement"
    assert all("Enterprise Service Level Agreement" not in s.text for s in document.segments)


def test_front_matter_is_promoted_to_metadata():
    document = load_markdown(MARKDOWN)
    assert document.metadata == {
        "Document owner": "Customer Success",
        "Last reviewed": "2026-02-02",
        "Applies to": "Enterprise plan customers only",
    }


def test_no_administrative_chunk_is_produced():
    """Regression test for the retrieval defect this fix exists to remove.

    Before the fix, the H1 plus the owner/date block became chunk-0 of every document.
    It carried the document's vocabulary but none of its meaning, so it outranked the
    passages that actually answered a question.
    """
    document = load_markdown(MARKDOWN)
    for segment in document.segments:
        assert "Document owner" not in segment.text
        assert "Last reviewed" not in segment.text
    assert document.segments[0].section == "Uptime commitment"


def test_real_intro_prose_before_the_first_section_is_kept():
    text = "# Title\n\n**Owner:** Someone\n\nThis intro genuinely explains the policy.\n\n## First\n\nBody.\n"
    document = load_markdown(text)
    assert document.segments[0].section is None
    assert "genuinely explains" in document.segments[0].text


def test_bold_lines_after_prose_starts_are_content_not_metadata():
    """Only the leading administrative block is stripped; bold body text survives."""
    text = "# Title\n\nIntro sentence.\n\n**Important:** this is a real emphasised claim.\n\n## First\n\nBody.\n"
    document = load_markdown(text)
    assert "**Important:** this is a real emphasised claim." in document.segments[0].text
    assert "Important" not in document.metadata


def test_yaml_front_matter_is_parsed():
    text = "---\ntitle: Refund Policy\nowner: Finance\n---\n\n## Scope\n\nBody text.\n"
    document = load_markdown(text)
    assert document.metadata == {"title": "Refund Policy", "owner": "Finance"}
    assert document.segments[0].section == "Scope"


def test_plain_text_is_one_unsectioned_segment():
    document = load_text("Just a flat policy file.")
    assert len(document.segments) == 1
    assert document.segments[0].section is None and document.segments[0].page is None


# ---------------------------------------------------------------------- chunk records


def test_chunk_records_carry_stable_ids_and_metadata():
    document = load_markdown(MARKDOWN)
    records = chunk_segments(
        "enterprise-sla.md", document.segments, full_text=MARKDOWN,
        document_title=document.title,
    )

    assert records, "expected at least one chunk"
    assert [r["chunk_id"] for r in records] == [
        f"enterprise-sla.md#chunk-{i}" for i in range(len(records))
    ]
    assert all(r["document_id"] == "enterprise-sla.md" for r in records)

    credits = [r for r in records if r["section"] == "Service credits"]
    assert credits, "the Service credits section should survive into a chunk"
    assert "never paid out in cash" in credits[0]["text"]


def test_document_title_is_carried_on_every_chunk():
    document = load_markdown(MARKDOWN)
    records = chunk_segments(
        "enterprise-sla.md", document.segments, document_title=document.title
    )
    assert all(r["document_title"] == "Enterprise Service Level Agreement" for r in records)


def test_page_metadata_survives_chunking():
    segments = [Segment(text=PARAGRAPH * 4, page=2)]
    records = chunk_segments("remote-work-policy.pdf", segments)
    assert records and all(r["page"] == 2 for r in records)


def test_char_offsets_point_back_into_the_source():
    document = load_markdown(MARKDOWN)
    records = chunk_segments("enterprise-sla.md", document.segments, full_text=MARKDOWN)
    located = [r for r in records if r["char_start"] is not None]
    assert located, "expected at least one chunk to be locatable in the source"
    for record in located:
        assert MARKDOWN[record["char_start"] : record["char_end"]] == record["text"]
