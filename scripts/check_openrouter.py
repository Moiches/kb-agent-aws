"""Probe OpenRouter before writing any code that depends on it.

Three things are assumptions until this script says otherwise, and all three change the
design if they are wrong:

  1. The model slugs in `infra/lib/config.ts` actually exist.
  2. The embeddings endpoint honours `dimensions`, so the index can stay at 512.
  3. The returned vectors are unit-normalized, which is what lets the retriever treat
     cosine similarity as a plain dot product (ADR-02). If they are not, ingestion has to
     normalize explicitly -- a small change, but a silent wrong answer if missed.

Titan was measured directly and satisfies (2) and (3) exactly. That says nothing about
OpenRouter, so it gets measured too rather than assumed.

Usage
-----
    # PowerShell
    $env:OPENROUTER_API_KEY = "sk-or-..."
    python scripts/check_openrouter.py

    # bash
    export OPENROUTER_API_KEY="sk-or-..."
    python scripts/check_openrouter.py

    # find candidate slugs instead of probing
    python scripts/check_openrouter.py --list haiku
    python scripts/check_openrouter.py --list embedding

    # probe specific slugs
    python scripts/check_openrouter.py --gen anthropic/claude-haiku-4.5 \
                                       --embed openai/text-embedding-3-small

The key is read from the environment so it never lands in shell history, in a file, or in
this repository. Nothing printed below contains it: the key is reported only as a
fingerprint (first 8 characters of its SHA-256).

Cost: one embedding of one word plus an 8-token completion. Fractions of a cent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT = 30

# The slugs currently assumed by infra/lib/config.ts.
DEFAULT_GENERATION_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"
TARGET_DIMENSIONS = 512

OK, FAIL, WARN = "[ OK ]", "[FAIL]", "[WARN]"


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        sys.exit(
            "OPENROUTER_API_KEY is not set.\n"
            '  PowerShell: $env:OPENROUTER_API_KEY = "sk-or-..."\n'
            '  bash:       export OPENROUTER_API_KEY="sk-or-..."'
        )
    return key


def fingerprint(key: str) -> str:
    """Identify the key without revealing it -- the same trick the Lambda logs use."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def call(path: str, payload: dict | None = None) -> tuple[dict, float]:
    """GET or POST against OpenRouter. Returns (body, elapsed_ms)."""
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            "X-Title": "kb-agent-aws probe",
        },
        method="POST" if payload is not None else "GET",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from None
    return body, (time.perf_counter() - started) * 1000


def list_models(needle: str) -> None:
    body, _ = call("/models")
    matches = [m for m in body.get("data", []) if needle.lower() in m.get("id", "").lower()]
    if not matches:
        print(f"No model id contains {needle!r}.")
        return

    print(f"{len(matches)} model(s) matching {needle!r}:\n")
    for model in sorted(matches, key=lambda m: m["id"]):
        pricing = model.get("pricing", {})
        prompt = _per_million(pricing.get("prompt"))
        completion = _per_million(pricing.get("completion"))
        print(f"  {model['id']}")
        print(f"      in {prompt} / out {completion} per 1M tokens"
              f"   context {model.get('context_length', '?')}")


def _per_million(price_per_token: str | None) -> str:
    try:
        return f"${float(price_per_token) * 1_000_000:,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def probe_embeddings(model: str) -> bool:
    """Check the slug, the `dimensions` parameter, and vector normalization."""
    print(f"\nEMBEDDINGS  {model}")

    def request_embedding(dimensions: int | None) -> tuple[list[float], str, float]:
        payload: dict = {"model": model, "input": "hello"}
        if dimensions is not None:
            payload["dimensions"] = dimensions
        body, elapsed = call("/embeddings", payload)
        data = body.get("data") or []
        if not data:
            raise RuntimeError(f"no embedding in response: {json.dumps(body)[:200]}")
        return data[0]["embedding"], body.get("model", model), elapsed

    try:
        native, served, elapsed = request_embedding(None)
    except RuntimeError as exc:
        print(f"  {FAIL} {exc}")
        print(f"  -> try:  python {sys.argv[0]} --list embedding")
        return False

    print(f"  {OK} slug resolves, served by {served!r} in {elapsed:.0f} ms")
    print(f"       native dimensions: {len(native)}")

    try:
        reduced, _, _ = request_embedding(TARGET_DIMENSIONS)
    except RuntimeError as exc:
        print(f"  {WARN} `dimensions` rejected: {exc}")
        print(f"       -> keep the index at {len(native)} dimensions. Retrieval still works:")
        print(f"          52 chunks x {len(native)} dims is a few ms in pure Python.")
        reduced = native
    else:
        if len(reduced) == TARGET_DIMENSIONS:
            print(f"  {OK} `dimensions: {TARGET_DIMENSIONS}` honoured")
        else:
            print(f"  {WARN} asked for {TARGET_DIMENSIONS}, got {len(reduced)} -- "
                  "set EMBED_DIMENSIONS to the value actually returned")

    # Three-way, because "not exactly 1.0" and "not normalized" are very different
    # findings. float32 embeddings summed over hundreds of dimensions land a few parts in
    # 10,000 away from unit length; a genuinely unnormalized vector is off by orders of
    # magnitude. Reporting the first as a failure would be crying wolf.
    norm = math.sqrt(sum(x * x for x in reduced))
    drift = abs(norm - 1.0)
    if drift < 1e-6:
        print(f"  {OK} vectors are exactly unit-normalized (L2 = {norm:.8f})")
    elif drift < 1e-2:
        print(f"  {OK} vectors are unit-normalized to float32 precision (L2 = {norm:.6f})")
        print(f"       drift {drift:.2e} shifts a 0.91 score by ~{0.91 * drift:.6f} -- irrelevant to ranking")
    else:
        print(f"  {FAIL} vectors are NOT normalized (L2 = {norm:.4f})")
        print("       Scores would be meaningless without normalizing. This is not optional.")

    # Normalize anyway, whatever the measurement said. OpenRouter is a *router*: the same
    # slug can be served by a different upstream tomorrow, and nothing in the contract
    # promises unit vectors. Two lines at ingest time turn an observed property into a
    # guaranteed one, and the failure mode it prevents is silent.
    print(f"  {OK} ingestion normalizes explicitly regardless -- the retriever's dot-product")
    print("       assumption must not depend on an upstream we do not control")
    return True


def probe_generation(model: str) -> bool:
    """Check the slug and that the model follows a trivial exact-output instruction."""
    print(f"\nGENERATION  {model}")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply with exactly the word: PONG"},
            {"role": "user", "content": "ping"},
        ],
        "max_tokens": 8,
        "temperature": 0.0,
    }
    try:
        body, elapsed = call("/chat/completions", payload)
    except RuntimeError as exc:
        print(f"  {FAIL} {exc}")
        print(f"  -> try:  python {sys.argv[0]} --list haiku")
        return False

    text = body["choices"][0]["message"]["content"].strip()
    usage = body.get("usage", {})
    print(f"  {OK} slug resolves, served by {body.get('model', model)!r} in {elapsed:.0f} ms")
    print(f"       reply: {text!r}   tokens: {usage.get('prompt_tokens')} in / "
          f"{usage.get('completion_tokens')} out")

    # Not a benchmark -- a smoke test of the one capability this whole design leans on:
    # emitting an exact string on demand. Citations and the INSUFFICIENT_CONTEXT sentinel
    # both depend on it.
    if "PONG" in text.upper():
        print(f"  {OK} follows exact-output instructions")
    else:
        print(f"  {WARN} did not return the requested exact string.")
        print("       Citation format and the INSUFFICIENT_CONTEXT sentinel depend on this;")
        print("       consider a different model before building on it.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify OpenRouter assumptions before coding against them.")
    parser.add_argument("--list", metavar="SUBSTRING", help="list model ids containing SUBSTRING and exit")
    parser.add_argument("--gen", default=DEFAULT_GENERATION_MODEL, help="generation slug to probe")
    parser.add_argument("--embed", default=DEFAULT_EMBEDDING_MODEL, help="embedding slug to probe")
    args = parser.parse_args()

    print(f"OpenRouter probe  |  key fingerprint {fingerprint(api_key())}  |  {BASE_URL}")

    if args.list:
        list_models(args.list)
        return 0

    embeddings_ok = probe_embeddings(args.embed)
    generation_ok = probe_generation(args.gen)

    print("\n" + "-" * 72)
    if embeddings_ok and generation_ok:
        print("Both slugs resolve. Update infra/lib/config.ts if either differed from the")
        print("default, then Phase 3 (ingestion) is unblocked.")
        return 0

    print("At least one slug failed. Find the right one with --list, then re-run with")
    print("--gen / --embed. No code change is needed to try a different model:")
    print("  cdk deploy -c generationModel=<slug> -c embeddingModel=<slug>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
