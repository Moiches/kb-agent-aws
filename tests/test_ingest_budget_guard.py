"""The corpus ceiling, which is a cost control before it is anything else.

Uploading is now a two-click operation, and every uploaded byte becomes chunks, chunks become
embeddings, and embeddings are billed against a fixed $20. A dropped directory or a 200 MB
scan should not be able to spend that, and it should not be able to spend it *before* anyone
notices -- so the check runs ahead of the first embedding call, not after.

Loaded by path: three services each ship a module called `handler`.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "services" / "ingest" / "handler.py"
_spec = importlib.util.spec_from_file_location("ingest_handler", _PATH)
ingest = importlib.util.module_from_spec(_spec)
sys.modules["ingest_handler"] = ingest
_spec.loader.exec_module(ingest)


@pytest.fixture
def wired(monkeypatch):
    """Everything up to the ceiling check is real; everything past it is instrumented."""
    state = {"embedded": 0, "written": 0}

    def _wire(chunk_count: int, limit: int = 2500):
        monkeypatch.setattr(ingest, "BUCKET", "test-bucket")
        monkeypatch.setattr(ingest, "MAX_INDEX_CHUNKS", limit)
        monkeypatch.setattr(ingest, "read_provider_key", lambda: "sk-test")
        monkeypatch.setattr(ingest, "list_documents", lambda: ["raw/a.md"])
        monkeypatch.setattr(
            ingest, "build_chunks",
            lambda keys: [{"chunk_id": f"a.md#chunk-{i}", "document_id": "a.md",
                           "text": "x"} for i in range(chunk_count)],
        )

        def spy_write(artifact):
            state["written"] += 1
            return 1234

        class SpyEmbedder:
            def __init__(self, **_kwargs):
                pass

            def embed_batch(self, texts):
                state["embedded"] += len(texts)
                return [[0.0, 1.0] for _ in texts]

        monkeypatch.setattr(ingest, "write_index", spy_write)
        monkeypatch.setattr(ingest, "OpenRouterEmbedder", SpyEmbedder)
        return state

    return _wire


def test_a_corpus_within_the_ceiling_is_indexed(wired):
    state = wired(chunk_count=10, limit=2500)
    result = ingest.lambda_handler({}, None)

    assert result["seeded"] is True
    assert result["chunks"] == 10
    assert state["embedded"] == 10
    assert state["written"] == 1


def test_a_corpus_over_the_ceiling_is_refused(wired):
    state = wired(chunk_count=2501, limit=2500)
    result = ingest.lambda_handler({}, None)

    assert result["seeded"] is False
    assert result["reason"] == "too_many_chunks"
    assert result["chunks"] == 2501
    assert result["limit"] == 2500


def test_nothing_is_embedded_when_the_ceiling_is_hit(wired):
    """The whole point of the ordering. Checking after embedding would report the refusal
    accurately and still have spent the money."""
    state = wired(chunk_count=9000, limit=2500)
    ingest.lambda_handler({}, None)

    assert state["embedded"] == 0


def test_the_previous_index_survives_a_refusal(wired):
    """A knowledge base that is out of date can be rebuilt. One overwritten by a partial
    run, or a budget already spent, cannot."""
    state = wired(chunk_count=9000, limit=2500)
    ingest.lambda_handler({}, None)

    assert state["written"] == 0


def test_the_ceiling_is_inclusive_at_the_limit(wired):
    state = wired(chunk_count=2500, limit=2500)
    result = ingest.lambda_handler({}, None)

    assert result["seeded"] is True
    assert state["written"] == 1


def test_an_empty_corpus_publishes_an_empty_index(wired, monkeypatch):
    """Deleting the last document has to take effect. Returning early here would leave the
    previous index in place, so every deleted document would stay searchable forever."""
    state = wired(chunk_count=0)
    monkeypatch.setattr(ingest, "list_documents", list)

    result = ingest.lambda_handler({}, None)

    assert result["seeded"] is True
    assert result["chunks"] == 0
    assert result["reason"] == "no_documents"
    # Written, not skipped: the empty index is the deletion taking effect.
    assert state["written"] == 1
    assert state["embedded"] == 0
