"""OpenRouter provider: the per-call model override on `generate`.

The verifier runs on a different slug than the answer writer, through the same client so it
inherits the retries and the error translation. These tests pin the payload construction:
the override reaches the wire, the default still applies when it is absent, and the reported
model is always what the response says served the call.

No network. `urlopen` is replaced so every case is deterministic.
"""

import json

from rag.providers import openrouter
from rag.providers.openrouter import OpenRouterProvider


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
    """Replace urlopen with a scripted sequence. Returns the list of captured payloads."""
    captured = []
    queue = list(responses)

    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        captured.append(json.loads(request.data.decode()))
        return FakeResponse(queue.pop(0))

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(openrouter.time, "sleep", lambda _seconds: None)
    return captured


def completion_response(text: str = "ok", model: str = "served/model") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }


def provider() -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key="k",
        generation_model="default/generator",
        embedding_model="default/embedder",
        embedding_dimensions=8,
    )


def test_the_configured_model_is_used_when_no_override_is_given(monkeypatch):
    """Every existing caller passes no `model`; their requests must not change."""
    captured = install_transport(monkeypatch, [completion_response()])
    provider().generate(system="s", user="u", max_tokens=50, temperature=0.2)
    assert captured[0]["model"] == "default/generator"


def test_an_override_reaches_the_wire_and_leaves_the_rest_of_the_payload_alone(monkeypatch):
    captured = install_transport(monkeypatch, [completion_response()])
    provider().generate(system="s", user="u", max_tokens=50, temperature=0.0, model="stronger/verifier")

    payload = captured[0]
    assert payload["model"] == "stronger/verifier"
    assert payload["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert payload["max_tokens"] == 50
    assert payload["temperature"] == 0.0


def test_the_override_is_per_call_and_does_not_stick(monkeypatch):
    """The verifier's slug must not leak into the next answer on a warm, cached provider."""
    captured = install_transport(monkeypatch, [completion_response(), completion_response()])
    client = provider()
    client.generate(system="s", user="u", max_tokens=50, temperature=0.0, model="stronger/verifier")
    client.generate(system="s", user="u", max_tokens=50, temperature=0.0)
    assert [c["model"] for c in captured] == ["stronger/verifier", "default/generator"]
    assert client.generation_model == "default/generator"


def test_an_empty_override_means_the_default(monkeypatch):
    """`VERIFY_MODEL_ID=""` in an environment should not send an empty slug upstream."""
    captured = install_transport(monkeypatch, [completion_response()])
    provider().generate(system="s", user="u", max_tokens=50, temperature=0.0, model="")
    assert captured[0]["model"] == "default/generator"


def test_the_result_reports_what_served_the_request_not_what_was_asked_for(monkeypatch):
    """OpenRouter routes; the response body is the only honest source for the log."""
    install_transport(monkeypatch, [completion_response(model="actually/routed-here")])
    result = provider().generate(system="s", user="u", max_tokens=50, temperature=0.0, model="stronger/verifier")
    assert result.model == "actually/routed-here"
    assert result.text == "ok"
    assert (result.input_tokens, result.output_tokens) == (12, 3)
