"""Gate G0: prove the checker can see the boundary inversion before building anything on it.

The verification loop exists for one measured failure. On 2026-09-08 the standard style
answered Q2 -- "What service credit applies if uptime drops to 99.0%?" -- with "no service
credit applies", cited the passage that says 99.0% earns 10%, and left with grounding
"high". The simple style, same passage, same day, got it right. If a fact-checker handed
that passage cannot tell those two answers apart, no orchestration around it will help, so
this script asks exactly that question and nothing else:

    python scripts/probe_verifier.py --secret-id kbagent-mc-dev/provider-api-key

    # or with the key already in the environment
    $env:OPENROUTER_API_KEY = "sk-or-..."      # PowerShell
    export OPENROUTER_API_KEY="sk-or-..."      # bash
    python scripts/probe_verifier.py

Both answers are read from the evaluation artifacts rather than pasted here, so the probe
cannot drift from what was actually observed, and the script refuses to run if either
fixture no longer says what it is claimed to say. The passage is enterprise-sla.md#chunk-2,
recomputed with the ingest Lambda's own loader and splitter so it is byte-identical to what
the index holds. Each answer is checked N times at temperature 0. Temperature 0 is not
deterministic at the provider, and a gate that passed once is a coin flip, not a gate.

Exit 0 only if the wrong answer is flagged `fail` in at least 4 of 5 runs AND the right
answer passes in at least 4 of 5. Anything else is exit 1, and the plan stops there.

The key is read from the environment or from Secrets Manager and is never printed; only
its SHA-256 fingerprint is, the same way the Lambda logs identify it. Cost: ten short
completions, about a cent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Both Lambda code roots, exactly as tests/conftest.py puts them on the path: `rag.*` for
# the verifier and the flat ingest modules for the loader and splitter.
for code_root in (ROOT / "services" / "query", ROOT / "services" / "ingest"):
    sys.path.insert(0, str(code_root))

from loaders import load_document  # noqa: E402
from rag.models import Chunk, Hit  # noqa: E402
from rag.verifier import FAIL, PASS, VERIFIER_PROMPT, build_verify_message, parse_verdict  # noqa: E402
from splitter import chunk_segments  # noqa: E402

BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT = 60
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_RUNS = 5
REQUIRED = 4

# The same budget the Lambda gives the verifier (VERIFY_MAX_TOKENS). A budget that truncates
# the JSON shows up here as `unverified`, which is the right place to find that out.
MAX_TOKENS = 400
MAX_PASSAGES = 4

# The frozen baseline is the record; results.json holds the same rows until the next
# evaluation run overwrites it, so it is only a fallback for a checkout made before the
# baseline was frozen.
BASELINE = ROOT / "evaluation" / "results-main-2026-09-08.json"
LIVE_RESULTS = ROOT / "evaluation" / "results.json"
AB_STYLES = ROOT / "evaluation" / "ab-styles.json"
SLA_DOCUMENT = ROOT / "sample-docs" / "enterprise-sla.md"
QUESTION_ID = "Q2"
PASSAGE_ID = "enterprise-sla.md#chunk-2"

WINDOWS_CLI = Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")


def aws_binary() -> str:
    return shutil.which("aws") or (str(WINDOWS_CLI) if WINDOWS_CLI.exists() else "")


def api_key(secret_id: str | None, profile: str | None) -> str:
    """An explicit --secret-id wins over the environment: whoever typed it meant it."""
    if secret_id:
        binary = aws_binary()
        if not binary:
            sys.exit("AWS CLI not found; set OPENROUTER_API_KEY instead of --secret-id.")
        command = [binary, "secretsmanager", "get-secret-value", "--secret-id", secret_id,
                   "--query", "SecretString", "--output", "text"]
        if profile:
            command += ["--profile", profile]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit(result.stderr.strip())
        return result.stdout.strip()

    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        sys.exit(
            "No key. Pass --secret-id kbagent-mc-dev/provider-api-key, or set OPENROUTER_API_KEY.\n"
            '  PowerShell: $env:OPENROUTER_API_KEY = "sk-or-..."\n'
            '  bash:       export OPENROUTER_API_KEY="sk-or-..."'
        )
    return key


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def load_fixtures() -> tuple[str, str, str, float]:
    """The question, the wrong standard answer, the right simple answer, and the top score
    the passage was retrieved with. All recorded, none retyped."""
    results_path = BASELINE if BASELINE.exists() else LIVE_RESULTS
    results = json.loads(results_path.read_text(encoding="utf-8"))["results"]
    wrong_row = next(row for row in results if row["id"] == QUESTION_ID)
    styles = json.loads(AB_STYLES.read_text(encoding="utf-8"))
    right_row = next(row for row in styles if row["id"] == QUESTION_ID)

    wrong, right = wrong_row["answer"], right_row["simple"]["answer"]
    # A probe that measured some other answer than the one it claims to would be worse than
    # no probe at all, so check the fixtures still are what the docstring says they are.
    if "no service credit" not in wrong.lower():
        sys.exit(f"{results_path.name}: the recorded Q2 answer is no longer the wrong one "
                 f"this probe exists for; restore the 2026-09-08 baseline.")
    if "10%" not in right:
        sys.exit(f"{AB_STYLES.name}: the recorded simple Q2 answer no longer states 10%.")

    print(f"fixtures: {results_path.name} (wrong, standard) + {AB_STYLES.name} (right, simple)")
    return wrong_row["question"], wrong, right, float(wrong_row["top_score"])


def passage(score: float) -> Hit:
    """enterprise-sla.md#chunk-2 exactly as the ingest Lambda would index it."""
    data = SLA_DOCUMENT.read_bytes()
    document = load_document(SLA_DOCUMENT.name, data)
    records = chunk_segments(
        SLA_DOCUMENT.name,
        document.segments,
        full_text=data.decode("utf-8", errors="replace"),
        document_title=document.title,
    )
    record = next(r for r in records if r["chunk_id"] == PASSAGE_ID)
    chunk = Chunk(
        chunk_id=record["chunk_id"],
        document_id=record["document_id"],
        text=record["text"],
        embedding=[],
        document_title=record["document_title"],
        section=record["section"],
        page=record["page"],
    )
    return Hit(chunk=chunk, score=score)


def verify_once(key: str, model: str, user: str) -> tuple[str, str, str, float]:
    """One checker call. Returns (verdict, issue, raw reply, elapsed_ms).

    No retries and no translation: a wrong key or a missing slug should stop the probe on
    the first call with the provider's own words, not after five identical failures.
    """
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": VERIFIER_PROMPT},
                {"role": "user", "content": user},
            ],
            "max_tokens": MAX_TOKENS,
            "temperature": 0.0,
        }).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "kb-agent-aws verifier probe",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        sys.exit(f"HTTP {exc.code} from {BASE_URL}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"network error: {exc.reason}")
    elapsed = (time.perf_counter() - started) * 1000

    choices = body.get("choices") or []
    raw = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    verdict = parse_verdict(raw)
    return verdict.verdict, verdict.issue, raw, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Can the verifier see the Q2 boundary inversion?")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="checker slug (default: %(default)s)")
    parser.add_argument("--secret-id", help="read the key from this Secrets Manager secret, "
                                            "e.g. kbagent-mc-dev/provider-api-key")
    parser.add_argument("--profile", help="AWS CLI profile for --secret-id")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help="checks per answer (default: %(default)s)")
    args = parser.parse_args()

    # The verdicts quote passage text back; a curly quote in one must not crash the probe
    # on a cp1252 console.
    sys.stdout.reconfigure(errors="replace")

    key = api_key(args.secret_id, args.profile)
    question, wrong, right, score = load_fixtures()
    hit = passage(score)
    print(f"passage: {PASSAGE_ID}, {len(hit.chunk.text)} chars, recomputed with the ingest splitter")
    print(f"checker: {args.model}  |  key fingerprint {fingerprint(key)}  |  {args.runs} runs each\n")

    answers = [
        ("wrong", wrong, FAIL, "flagged"),
        ("right", right, PASS, "passed"),
    ]
    outcomes: dict[str, list[str]] = {}
    for label, answer, wanted, _ in answers:
        user = build_verify_message(question, answer, [hit], MAX_PASSAGES)
        outcomes[label] = []
        for run in range(1, args.runs + 1):
            verdict, issue, raw, elapsed = verify_once(key, args.model, user)
            outcomes[label].append(verdict)
            mark = "ok  " if verdict == wanted else "MISS"
            print(f"  {mark} {label} #{run}: {verdict:<10} {elapsed:6.0f} ms  {issue}")
            if verdict not in (PASS, FAIL):
                # Unparsable is the one outcome the table cannot explain by itself.
                print(f"         raw reply: {raw[:300]!r}")

    print(f"\n  {'answer':<8}" + "".join(f"{f'#{n}':<12}" for n in range(1, args.runs + 1)))
    counts: dict[str, int] = {}
    for label, _, wanted, word in answers:
        counts[label] = sum(1 for v in outcomes[label] if v == wanted)
        row = "".join(f"{v:<12}" for v in outcomes[label])
        print(f"  {label:<8}{row}{counts[label]}/{args.runs} {word}")

    print()
    if counts["wrong"] >= REQUIRED and counts["right"] >= REQUIRED:
        print(f"G0 PASSED: the checker sees the boundary inversion ({counts['wrong']}/{args.runs}) "
              f"and does not cry wolf ({counts['right']}/{args.runs}). The loop is worth building.")
        return 0
    print(f"G0 FAILED: needed >= {REQUIRED}/{args.runs} flagged AND >= {REQUIRED}/{args.runs} passed. "
          "Stop here -- an orchestration loop around a checker that cannot see this "
          "failure would only add cost.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
