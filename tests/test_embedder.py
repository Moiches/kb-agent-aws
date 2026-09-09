"""Embedding client: normalization, batching, ordering and retry behaviour.

No network. `urlopen` is replaced so every case is deterministic.
"""

import json
import math
import urllib.error

import embedder
import pytest
from embedder import EmbeddingError, OpenRouterEmbedder, normalize


class FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def install_transport(monkeypatch, responses):
    """Replace urlopen with a scripted sequence. Returns the list of captured requests."""
    captured = []
    queue = list(responses)

    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        captured.append(json.loads(request.data.decode()))
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(embedder.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(embedder.time, "sleep", lambda _seconds: None)
    return captured


def embedding_response(vectors: list[list[float]], indices: list[int] | None = None) -> dict:
    indices = indices if indices is not None else list(range(len(vectors)))
    return {"data": [{"index": i, "embedding": v} for i, v in zip(indices, vectors)]}


def http_error(code: int) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError("https://x", code, "boom", {}, io.BytesIO(b"{}"))


# --------------------------------------------------------------------------- normalize


def test_normalize_produces_a_unit_vector():
    result = normalize([3.0, 4.0])
    assert math.isclose(math.sqrt(sum(x * x for x in result)), 1.0, abs_tol=1e-12)
    assert result == pytest.approx([0.6, 0.8])


def test_normalize_preserves_direction():
    original = [1.0, -2.0, 3.0]
    result = normalize(original)
    ratios = [r / o for r, o in zip(result, original)]
    assert ratios == pytest.approx([ratios[0]] * 3)


def test_normalize_leaves_a_zero_vector_alone_instead_of_dividing_by_zero():
    assert normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


def test_an_already_normalized_vector_survives_unchanged():
    assert normalize([1.0, 0.0]) == pytest.approx([1.0, 0.0])


# ---------------------------------------------------------------------------- embedding


def test_returned_vectors_are_always_normalized(monkeypatch):
    """The provider's normalization is not trusted -- OpenRouter can change upstream."""
    install_transport(monkeypatch, [embedding_response([[6.0, 8.0]])])
    vector = OpenRouterEmbedder("k").embed("hello")
    assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, abs_tol=1e-12)


def test_the_request_carries_model_and_dimensions(monkeypatch):
    captured = install_transport(monkeypatch, [embedding_response([[1.0, 0.0]])])
    OpenRouterEmbedder("k", model="some/model", dimensions=512).embed("hello")
    assert captured[0]["model"] == "some/model"
    assert captured[0]["dimensions"] == 512
    assert captured[0]["input"] == ["hello"]


def test_out_of_order_responses_are_realigned_by_index(monkeypatch):
    """Trusting array order would silently give every chunk someone else's vector."""
    install_transport(
        monkeypatch,
        [embedding_response([[0.0, 1.0], [1.0, 0.0]], indices=[1, 0])],
    )
    vectors = OpenRouterEmbedder("k").embed_batch(["first", "second"])
    assert vectors[0] == pytest.approx([1.0, 0.0])
    assert vectors[1] == pytest.approx([0.0, 1.0])


def test_batches_are_split_and_results_concatenated_in_order(monkeypatch):
    captured = install_transport(
        monkeypatch,
        [
            embedding_response([[1.0, 0.0], [0.0, 1.0]]),
            embedding_response([[1.0, 1.0]]),
        ],
    )
    vectors = OpenRouterEmbedder("k", batch_size=2).embed_batch(["a", "b", "c"])

    assert [c["input"] for c in captured] == [["a", "b"], ["c"]]
    assert len(vectors) == 3
    assert vectors[0] == pytest.approx([1.0, 0.0])
    assert vectors[2] == pytest.approx([0.7071067, 0.7071067], abs=1e-6)


def test_a_short_response_is_an_error_rather_than_a_silent_mismatch(monkeypatch):
    install_transport(monkeypatch, [embedding_response([[1.0, 0.0]])])
    with pytest.raises(EmbeddingError, match="expected 2 embeddings"):
        OpenRouterEmbedder("k").embed_batch(["a", "b"])


def test_an_empty_api_key_is_rejected_at_construction():
    with pytest.raises(EmbeddingError, match="no API key"):
        OpenRouterEmbedder("")


# ------------------------------------------------------------------------------ retries


def test_rate_limits_are_retried(monkeypatch):
    install_transport(monkeypatch, [http_error(429), embedding_response([[1.0, 0.0]])])
    assert OpenRouterEmbedder("k").embed("hello") == pytest.approx([1.0, 0.0])


def test_server_errors_are_retried(monkeypatch):
    install_transport(monkeypatch, [http_error(503), http_error(500), embedding_response([[0.0, 1.0]])])
    assert OpenRouterEmbedder("k").embed("hello") == pytest.approx([0.0, 1.0])


def test_client_errors_are_not_retried(monkeypatch):
    """A bad request or a rejected key will not fix itself; retrying just burns time."""
    captured = install_transport(monkeypatch, [http_error(401), embedding_response([[1.0, 0.0]])])
    with pytest.raises(EmbeddingError, match="HTTP 401"):
        OpenRouterEmbedder("k").embed("hello")
    assert len(captured) == 1


def test_retries_are_bounded(monkeypatch):
    install_transport(monkeypatch, [http_error(429)] * embedder.MAX_ATTEMPTS)
    with pytest.raises(EmbeddingError, match="giving up after"):
        OpenRouterEmbedder("k").embed("hello")


def test_network_failures_are_retried(monkeypatch):
    install_transport(
        monkeypatch,
        [urllib.error.URLError("connection reset"), embedding_response([[1.0, 0.0]])],
    )
    assert OpenRouterEmbedder("k").embed("hello") == pytest.approx([1.0, 0.0])
