"""The index cache must serve from memory, but not forever.

The bug these tests pin: re-running the ingest wrote a new artifact, and a warm Lambda kept
answering from the one it loaded at cold start. A document added to the knowledge base
looked like it had been ignored, and nothing in the logs said otherwise.
"""

import gzip
import json
import time

import pytest
from rag import config, index_store


def artifact(version: str, chunk_ids: list[str]) -> bytes:
    return gzip.compress(json.dumps({
        "kb_version": version,
        "embedding_model": "openai/text-embedding-3-small",
        "dimensions": 2,
        "document_count": 1,
        "chunks": [
            {"chunk_id": cid, "document_id": cid.split("#")[0],
             "text": f"text of {cid}", "embedding": [1.0, 0.0]}
            for cid in chunk_ids
        ],
    }).encode())


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class NoSuchKey(Exception):
    pass


class FakeS3:
    """Counts calls, so a test can assert that memory was *not* touched by the network."""

    class exceptions:  # noqa: N801 -- mirrors the botocore client attribute
        NoSuchKey = NoSuchKey

    def __init__(self, version: str, chunk_ids: list[str], etag: str):
        self.version, self.chunk_ids, self.etag = version, chunk_ids, etag
        self.gets = 0
        self.heads = 0
        self.head_raises: Exception | None = None

    def publish(self, version: str, chunk_ids: list[str], etag: str) -> None:
        """Stand in for a re-ingest replacing the artifact."""
        self.version, self.chunk_ids, self.etag = version, chunk_ids, etag

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 -- boto3 casing
        self.gets += 1
        return {"Body": FakeBody(artifact(self.version, self.chunk_ids)), "ETag": self.etag}

    def head_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        self.heads += 1
        if self.head_raises:
            raise self.head_raises
        return {"ETag": self.etag}


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3("v1", ["doc.md#chunk-0"], etag='"aaa"')
    monkeypatch.setattr(index_store, "_s3", fake)
    index_store.reset()
    yield fake
    index_store.reset()


def expire_the_check_window() -> None:
    """Pretend the refresh interval has elapsed, without sleeping through it."""
    index_store._checked_at = time.monotonic() - config.INDEX_REFRESH_SECONDS - 1


def test_first_call_downloads(s3):
    kb = index_store.load("bucket", "index/kb.json.gz")
    assert kb.chunk_count == 1
    assert s3.gets == 1


def test_a_warm_environment_answers_from_memory(s3):
    index_store.load("bucket", "index/kb.json.gz")
    for _ in range(5):
        index_store.load("bucket", "index/kb.json.gz")
    # Within the refresh window nothing is downloaded and nothing is even checked: the
    # whole point of the in-memory index is that retrieval costs nothing.
    assert (s3.gets, s3.heads) == (1, 0)


def test_a_reingest_is_picked_up(s3):
    """The regression test. Without this, seeding a new document changes nothing."""
    index_store.load("bucket", "index/kb.json.gz")
    s3.publish("v2", ["doc.md#chunk-0", "new.pdf#chunk-0"], etag='"bbb"')

    expire_the_check_window()
    kb = index_store.load("bucket", "index/kb.json.gz")

    assert kb.chunk_count == 2
    assert kb.kb_version == "v2"
    assert s3.gets == 2


def test_an_unchanged_artifact_is_not_downloaded_again(s3):
    """A HEAD is cheap; re-downloading and re-parsing the index is not."""
    index_store.load("bucket", "index/kb.json.gz")
    expire_the_check_window()
    index_store.load("bucket", "index/kb.json.gz")
    assert (s3.gets, s3.heads) == (1, 1)


def test_a_failed_check_keeps_serving_the_cached_index(s3):
    """If S3 is unreachable, a possibly-stale answer beats no answer at all."""
    index_store.load("bucket", "index/kb.json.gz")
    s3.head_raises = RuntimeError("S3 unreachable")

    expire_the_check_window()
    kb = index_store.load("bucket", "index/kb.json.gz")

    assert kb.chunk_count == 1
    assert s3.gets == 1


def test_the_check_can_be_switched_off(s3, monkeypatch):
    """`INDEX_REFRESH_SECONDS=0` restores load-once behaviour for anyone who wants it."""
    monkeypatch.setattr(config, "INDEX_REFRESH_SECONDS", 0)
    index_store.load("bucket", "index/kb.json.gz")
    s3.publish("v2", ["doc.md#chunk-0", "new.pdf#chunk-0"], etag='"bbb"')

    index_store._checked_at = 0.0  # as stale as it can be
    kb = index_store.load("bucket", "index/kb.json.gz")

    assert kb.kb_version == "v1"
    assert s3.heads == 0


def test_a_missing_index_is_a_state_not_a_crash(s3):
    def raise_missing(Bucket, Key):  # noqa: N803
        raise NoSuchKey()

    s3.get_object = raise_missing
    with pytest.raises(index_store.IndexUnavailable):
        index_store.load("bucket", "index/kb.json.gz")
