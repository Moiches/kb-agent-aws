"""Run the whole RAG pipeline locally, with no AWS calls at all.

Purpose: a fast iteration loop for chunking, retrieval, prompting and confidence before
any of it is deployed. It exercises the exact modules the Lambdas import.

    python scripts/local_rag_demo.py                # index + run the evaluation questions
    python scripts/local_rag_demo.py "your question"

IMPORTANT CAVEAT ON SCORES
--------------------------
Embeddings here come from a deterministic hashing bag-of-words stand-in, not from Titan.
That is enough to prove the pipeline wires together and to see which documents a question
pulls, but the *absolute* similarity values are on a different scale from Titan's.
RELEVANCE_FLOOR and CONFIDENCE_CEIL therefore CANNOT be calibrated from this script --
that has to happen against the deployed system (Phase 7).
"""

from __future__ import annotations

import hashlib
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "query"))
sys.path.insert(0, str(ROOT / "services" / "ingest"))

from loaders import SUPPORTED_EXTENSIONS, load_document  # noqa: E402
from rag.confidence import compute_confidence, grounding_label  # noqa: E402
from rag.models import Chunk  # noqa: E402
from rag.prompt import build_user_message  # noqa: E402
from rag.retriever import VectorStore  # noqa: E402
from splitter import chunk_segments  # noqa: E402

DIMENSIONS = 512
FLOOR, CEIL = 0.30, 0.75
TOP_K, MAX_PER_DOC = 5, 3

SAMPLE_QUESTIONS = [
    "How long does an Enterprise customer have to request a refund?",
    "What service credit applies if uptime drops to 99.0%?",
    "Where is customer data stored, and is it encrypted at rest?",
    "What is the response time for a Severity 1 incident?",
    "How do I set up SAML single sign-on?",
    "What happens if I exceed my monthly API request quota?",
    "Who do I page if the on-call engineer does not respond within 15 minutes?",
    "Can I still get a refund 45 days after my invoice?",
    "What is the difference between a refund and a service credit?",
    "Do you sign HIPAA Business Associate Agreements?",
    "How much is the home office allowance for remote employees?",
]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "or", "in", "on",
    "for", "with", "at", "by", "from", "as", "that", "this", "it", "be", "do", "does",
    "i", "you", "we", "my", "your", "our", "if", "not", "no", "can", "will", "how",
    "what", "when", "who", "where", "which",
}


def fake_embed(text: str) -> list[float]:
    """Deterministic hashing bag-of-words embedding. Stands in for Titan locally."""
    vector = [0.0] * DIMENSIONS
    for token in _TOKEN_RE.findall(text.lower()):
        if token in _STOPWORDS or len(token) < 3:
            continue
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def build_index() -> tuple[VectorStore, int]:
    docs_dir = ROOT / "sample-docs"
    paths = sorted(p for p in docs_dir.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)
    if not paths:
        raise SystemExit(f"no documents found in {docs_dir}")

    chunks: list[Chunk] = []
    print(f"Indexing {len(paths)} documents from {docs_dir.name}/\n")

    for path in paths:
        data = path.read_bytes()
        document = load_document(path.name, data)
        full_text = data.decode("utf-8", errors="replace") if path.suffix != ".pdf" else ""
        records = chunk_segments(
            path.name, document.segments, full_text=full_text,
            document_title=document.title,
        )

        for record in records:
            chunks.append(Chunk.from_record({**record, "embedding": fake_embed(record["text"])}))

        sizes = [len(r["text"]) for r in records]
        avg = sum(sizes) // len(sizes) if sizes else 0
        print(f"  {path.name:<42} {len(document.segments):>3} segments -> {len(records):>3} chunks "
              f"(avg {avg} chars)")

    print(f"\nTotal: {len(chunks)} chunks across {len(paths)} documents")
    return VectorStore(chunks), len(paths)


def answer(store: VectorStore, question: str) -> None:
    hits = store.search(fake_embed(question), top_k=TOP_K, max_per_document=MAX_PER_DOC)
    scores = [h.score for h in hits]
    top_score = scores[0] if scores else 0.0

    # No LLM locally, so assume the model would cite every passage it was given. This
    # isolates the retrieval half of the confidence score.
    retrieved_ids = [h.chunk.chunk_id for h in hits]
    confidence, components = compute_confidence(
        scores, retrieved_ids, retrieved_ids, floor=FLOOR, ceil=CEIL
    )
    label = grounding_label(confidence, top_score, floor=FLOOR, abstained=False)

    print(f"\n{'=' * 78}\nQ: {question}")
    print(f"   grounding={label}  confidence={confidence}  top_score={top_score:.3f}  "
          f"components={components}")

    if not hits:
        print("   (no hits)")
        return

    for rank, hit in enumerate(hits, 1):
        where = hit.chunk.section or (f"page {hit.chunk.page}" if hit.chunk.page else "-")
        excerpt = " ".join(hit.chunk.text.split())[:96]
        print(f"   {rank}. {hit.score:.3f}  {hit.chunk.chunk_id}")
        print(f"      [{where}] {excerpt}...")


def main() -> None:
    store, _ = build_index()
    questions = sys.argv[1:] or SAMPLE_QUESTIONS

    for question in questions:
        answer(store, question)

    print(f"\n{'=' * 78}")
    print("Prompt that would be sent to Bedrock for the first question:\n")
    hits = store.search(fake_embed(questions[0]), top_k=TOP_K, max_per_document=MAX_PER_DOC)
    message = build_user_message(questions[0], hits)
    print(message[:1200] + ("\n... [truncated]" if len(message) > 1200 else ""))
    print(f"\nContext block is {len(message):,} chars (~{len(message) // 4:,} tokens).")


if __name__ == "__main__":
    main()
