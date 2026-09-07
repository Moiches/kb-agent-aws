"""Recursive character text splitter.

Written by hand rather than pulled from LangChain for two reasons:

1.  The ingest Lambda stays at one third-party dependency (`pypdf`), which keeps the
    deployment package small and buildable without Docker.
2.  Chunking strategy is something a reviewer should be able to read and judge in one
    sitting. Forty lines of explicit code beats a framework call whose behaviour has to
    be looked up.

The algorithm is the standard recursive one: try to split on the most semantic separator
available ("\\n## " for a Markdown heading), fall back to progressively less semantic ones
("\\n\\n", "\\n", ". ", " "), and only ever cut mid-word as a last resort.
"""

from __future__ import annotations

# Flat module layout: the ingest Lambda's code root is on sys.path, so absolute imports
# are what run in Lambda and in tests alike.
from loaders import Segment

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 120

# Ordered from most to least semantically meaningful.
DEFAULT_SEPARATORS = ["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""]


def split_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    separators: list[str] | None = None,
) -> list[str]:
    """Split `text` into overlapping chunks of at most `chunk_size` characters."""
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    separators = list(DEFAULT_SEPARATORS if separators is None else separators)
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    # Pick the first separator that actually occurs in this text.
    separator, remaining = "", []
    for index, candidate in enumerate(separators):
        if candidate == "":
            separator, remaining = "", []
            break
        if candidate in text:
            separator, remaining = candidate, separators[index + 1 :]
            break

    pieces = text.split(separator) if separator else list(text)

    chunks: list[str] = []
    pending: list[str] = []
    for piece in pieces:
        if len(piece) <= chunk_size:
            pending.append(piece)
            continue
        # This piece is oversized on its own: flush what we have, then recurse into it
        # with the less-semantic separators that are left.
        if pending:
            chunks.extend(_merge(pending, separator, chunk_size, chunk_overlap))
            pending = []
        if remaining:
            chunks.extend(split_text(piece, chunk_size, chunk_overlap, remaining))
        else:
            chunks.extend(_hard_split(piece, chunk_size, chunk_overlap))

    if pending:
        chunks.extend(_merge(pending, separator, chunk_size, chunk_overlap))

    return [c for c in chunks if c.strip()]


def _merge(pieces: list[str], separator: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Greedily pack `pieces` back together into chunks, carrying an overlap tail."""
    sep_len = len(separator)
    chunks: list[str] = []
    window: list[str] = []
    length = 0

    for piece in pieces:
        addition = len(piece) + (sep_len if window else 0)
        if window and length + addition > chunk_size:
            chunks.append(separator.join(window).strip())
            # Drop from the front until the tail fits inside the overlap budget. This
            # tail becomes the start of the next chunk, which is what makes chunks
            # overlap and keeps a sentence that straddles a boundary retrievable.
            while window and length > chunk_overlap:
                removed = window.pop(0)
                length -= len(removed) + (sep_len if window else 0)
            addition = len(piece) + (sep_len if window else 0)
        window.append(piece)
        length += addition

    if window:
        chunks.append(separator.join(window).strip())
    return chunks


def _hard_split(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Last resort: fixed-width slices. Only reached when a single token exceeds chunk_size."""
    step = chunk_size - chunk_overlap
    return [text[i : i + chunk_size] for i in range(0, len(text), step)]


def chunk_segments(
    document_id: str,
    segments: list[Segment],
    full_text: str = "",
    document_title: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[dict]:
    """Turn loaded segments into the chunk records that go into the index artifact.

    The returned dicts are the on-disk contract between the ingest Lambda and the query
    Lambda; the two services share this schema, not any code.

    `document_title` comes from the front matter the loader stripped out. It is carried
    on every chunk so a citation can read "Enterprise SLA > Service credits" rather than
    "enterprise-sla.md > Service credits".
    """
    records: list[dict] = []
    cursor = 0

    for segment in segments:
        for piece in split_text(segment.text, chunk_size, chunk_overlap):
            start, end = _locate(full_text, piece, cursor)
            if start is not None:
                cursor = start + 1
            records.append(
                {
                    "chunk_id": f"{document_id}#chunk-{len(records)}",
                    "document_id": document_id,
                    "document_title": document_title,
                    "section": segment.section,
                    "page": segment.page,
                    "text": piece,
                    "char_start": start,
                    "char_end": end,
                }
            )
    return records


def _locate(haystack: str, needle: str, from_index: int) -> tuple[int | None, int | None]:
    """Best-effort character offsets of a chunk inside the original document.

    Returns (None, None) when the chunk cannot be located verbatim, which happens when
    the splitter normalised whitespace. Offsets are a debugging convenience, so an
    approximate answer is not worth extra machinery.
    """
    if not haystack:
        return None, None
    start = haystack.find(needle, from_index)
    if start == -1:
        start = haystack.find(needle)
    if start == -1:
        return None, None
    return start, start + len(needle)
