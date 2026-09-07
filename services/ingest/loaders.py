"""Document loaders for the knowledge base.

Each loader turns raw bytes into a `LoadedDocument`: a list of `Segment`s plus the
document-level title and metadata.

A segment is a span of text that carries the locality metadata we want to surface in a
citation:

  * Markdown -> one segment per heading section, so a citation can say
    "Enterprise SLA > Service credits" instead of "chunk 7".
  * PDF      -> one segment per page, so a citation can carry a page number.
  * Plain text -> a single segment.

**Front matter is deliberately excluded from the retrievable text.** Every document in
this corpus opens with an administrative block:

    # Refund and Cancellation Policy
    **Document owner:** Finance Operations
    **Last reviewed:** 2026-01-15

Left in place, that block becomes its own chunk. It carries the document's vocabulary
but none of its meaning, so it scores well on any question phrased in that vocabulary
and outranks the passage that actually answers it. This was measured on the local
harness before the fix: the question "How long does an Enterprise customer have to
request a refund?" retrieved `enterprise-sla.md#chunk-0` -- a title and a review date --
above the refund policy itself.

So the title and the key/value block are parsed into document metadata and dropped from
the text. Genuine prose before the first sub-heading is kept.

Only `pypdf` is required beyond the standard library, and it is pure Python, so the
ingest Lambda can be packaged with `pip install -t` on any OS without Docker.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

SUPPORTED_EXTENSIONS = (".md", ".markdown", ".txt", ".pdf")

# ATX headings: "## Service credits". Level 1 is the document title, not a section.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

# Front-matter entries, in either of the two common Markdown spellings:
#   **Document owner:** Finance Operations
#   **Document owner**: Finance Operations
_METADATA_RE = re.compile(r"^\*\*(?P<key>.+?)\*\*\s*:?\s*(?P<value>.*)$")

_YAML_FENCE = "---"


@dataclass
class Segment:
    """A span of source text plus where it came from."""

    text: str
    section: str | None = None
    page: int | None = None


@dataclass
class LoadedDocument:
    """A parsed document: retrievable segments plus non-retrievable metadata."""

    segments: list[Segment] = field(default_factory=list)
    title: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class UnsupportedDocumentError(ValueError):
    """Raised for a file extension the knowledge base does not handle."""


def load_document(filename: str, data: bytes) -> LoadedDocument:
    """Dispatch to the right loader based on the file extension."""
    lower = filename.lower()
    if lower.endswith((".md", ".markdown")):
        return load_markdown(data.decode("utf-8", errors="replace"))
    if lower.endswith(".txt"):
        return load_text(data.decode("utf-8", errors="replace"))
    if lower.endswith(".pdf"):
        return load_pdf(data)
    raise UnsupportedDocumentError(
        f"{filename}: expected one of {', '.join(SUPPORTED_EXTENSIONS)}"
    )


def load_text(text: str) -> LoadedDocument:
    text = text.strip()
    return LoadedDocument(segments=[Segment(text=text)] if text else [])


def load_markdown(text: str) -> LoadedDocument:
    """Split Markdown into one segment per heading section.

    The heading in force is carried on every segment so it can be shown in a citation.
    The H1 and any front-matter key/value block are lifted out into document metadata
    rather than indexed -- see the module docstring for why.
    """
    lines = text.splitlines()
    title: str | None = None
    metadata: dict[str, str] = {}
    start = 0

    # Optional YAML-style front matter, for documents that use that convention.
    if lines and lines[0].strip() == _YAML_FENCE:
        for index in range(1, len(lines)):
            if lines[index].strip() == _YAML_FENCE:
                start = index + 1
                break
            key, separator, value = lines[index].partition(":")
            if separator:
                metadata[key.strip()] = value.strip()

    segments: list[Segment] = []
    current_section: str | None = None
    buffer: list[str] = []
    in_preamble = True

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            segments.append(Segment(text=body, section=current_section))
        buffer.clear()

    for line in lines[start:]:
        heading = _HEADING_RE.match(line)
        if heading:
            level, heading_text = len(heading.group(1)), heading.group(2).strip()
            if level == 1 and title is None and in_preamble:
                title = heading_text
                continue  # promoted to document metadata, not indexed
            flush()
            in_preamble = False
            current_section = heading_text
            # Keep the heading inside the section body: it is often the most
            # semantically loaded phrase in the section and it helps retrieval.
            buffer.append(line)
            continue

        # Strip the administrative block only while it is still the administrative
        # block -- once real prose has started, a bold line is content, not metadata.
        if in_preamble and not buffer:
            stripped = line.strip()
            if not stripped:
                continue
            entry = _METADATA_RE.match(stripped)
            if entry:
                key = entry.group("key").rstrip(":").strip()
                metadata[key] = entry.group("value").strip()
                continue

        buffer.append(line)

    flush()
    return LoadedDocument(segments=segments, title=title, metadata=metadata)


def load_pdf(data: bytes) -> LoadedDocument:
    """One segment per page, so citations can carry a page number.

    Front-matter stripping is Markdown-only: a PDF has no reliable structure to key
    off, and its header line is one line inside a much larger page chunk, so it dilutes
    the embedding far less than a standalone metadata chunk did.
    """
    from pypdf import PdfReader  # imported lazily: only the ingest Lambda needs it

    reader = PdfReader(io.BytesIO(data))

    title = None
    if reader.metadata and reader.metadata.title:
        title = str(reader.metadata.title).strip() or None

    segments: list[Segment] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            segments.append(Segment(text=text, page=page_number))

    return LoadedDocument(segments=segments, title=title)
