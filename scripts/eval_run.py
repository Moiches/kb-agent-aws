"""Run the evaluation set against the deployed API and record what happened.

    python scripts/eval_run.py --api-url ... --token ...
    python scripts/eval_run.py --stack kbagent-mc-dev        # reads both from AWS
    python scripts/eval_run.py --stack kbagent-lg-dev --out evaluation/runs/L1-r1.json
    python scripts/eval_run.py --questions evaluation/questions-longdoc.json --style simple

Produces two artifacts:

  evaluation/results.json   every response in full, for calibration
  evaluation/results.md     the table that goes into EVALUATION.md

`--out` moves both: the markdown lands beside the JSON under the same stem, so a run written
to evaluation/runs/M-r1.json can never overwrite the committed results.md. Without it the
paths above apply and nothing about the original invocation changes. Files written this way
are what `scripts/eval_compare.py` reads to put two deployments side by side.

It does not decide whether an answer is *correct*. Correctness against `correct_answer`
needs a human read, and a script that graded itself would just be the same model marking
its own work. What it does measure is everything mechanical: which documents were
retrieved, what the scores were, whether the system abstained, and how confident it claimed
to be. Those are what the relevance floor is calibrated from.

Cost: eleven generations, roughly two cents.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "evaluation" / "questions.json"
RESULTS_JSON = ROOT / "evaluation" / "results.json"

WINDOWS_CLI = Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")


def aws_binary() -> str:
    return shutil.which("aws") or (str(WINDOWS_CLI) if WINDOWS_CLI.exists() else "")


def from_stack(stack: str, profile: str | None) -> tuple[str, str]:
    binary = aws_binary()
    if not binary:
        sys.exit("AWS CLI not found; pass --api-url and --token instead.")

    def run(args: list[str]) -> str:
        command = [binary, *args] + (["--profile", profile] if profile else [])
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit(result.stderr.strip())
        return result.stdout.strip()

    outputs = json.loads(
        run(["cloudformation", "describe-stacks", "--stack-name", stack,
             "--query", "Stacks[0].Outputs", "--output", "json"]) or "[]"
    )
    values = {o["OutputKey"]: o["OutputValue"] for o in outputs}
    token = run(["secretsmanager", "get-secret-value", "--secret-id",
                 values.get("ApiTokenSecret", f"{stack}/api-token"),
                 "--query", "SecretString", "--output", "text"])
    return values["ApiBaseUrl"], token


def ask(api_url: str, token: str, question: str, top_k: int,
        style: str = "standard") -> tuple[int, dict, float]:
    payload = json.dumps(
        {"question": question, "session_id": "evaluation", "top_k": top_k, "style": style}
    ).encode()
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/query",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read())
            status = response.status
    except urllib.error.HTTPError as exc:
        body, status = json.loads(exc.read() or b"{}"), exc.code
    except Exception as exc:  # noqa: BLE001
        body, status = {"error": "client", "message": str(exc)}, 0
    return status, body, (time.perf_counter() - started) * 1000


def evaluate(case: dict, status: int, body: dict) -> dict:
    """Score the mechanical parts. Factual correctness stays a human judgement."""
    sources = body.get("sources", [])
    documents = [s["document_id"] for s in sources]
    scores = [s["score"] for s in sources]
    metadata = body.get("metadata", {})
    # Additive, and only present on a deployment that verifies its answers. On main it is
    # absent and every field read from it below comes out None.
    verification = metadata.get("verification") or {}
    abstained = metadata.get("abstained", False)
    expected = case["expect_documents"]

    if case["expect"] == "abstain":
        behaviour_ok = abstained
    else:
        behaviour_ok = not abstained

    return {
        "id": case["id"],
        "question": case["question"],
        "expect": case["expect"],
        "tests": case["tests"],
        "correct_answer": case["correct_answer"],
        "status": status,
        "answer": body.get("answer", ""),
        "confidence": body.get("confidence"),
        "grounding": body.get("grounding"),
        "abstained": abstained,
        "top_score": round(scores[0], 4) if scores else 0.0,
        "scores": [round(s, 4) for s in scores],
        "documents": documents,
        # The retrieved set at chunk granularity. Document names cannot tell two runs apart
        # when the same document supplied a different passage, and "did retrieval move at
        # all" is the first thing a comparison between deployments has to settle.
        "chunk_ids": [s["chunk_id"] for s in sources],
        "top_document": documents[0] if documents else None,
        "expected_documents": expected,
        # Did the document that holds the answer make it into the results at all?
        "expected_retrieved": bool(expected) and any(d in documents for d in expected),
        # And did it rank first? Retrieving the right document but ranking the wrong one
        # first is a different, milder problem.
        "expected_ranked_first": bool(documents) and documents[0] in expected,
        "cited_count": sum(1 for s in sources if s.get("cited")),
        "source_count": len(sources),
        "behaviour_ok": behaviour_ok,
        "components": metadata.get("confidence_components", {}),
        # Taken from the response rather than the flag, so the file says what the server
        # actually used.
        "style": metadata.get("style"),
        "latency_ms": metadata.get("latency_ms"),
        "generation_ms": metadata.get("generation_ms"),
        "input_tokens": metadata.get("input_tokens", 0),
        "output_tokens": metadata.get("output_tokens", 0),
        "cost_usd": metadata.get("estimated_cost_usd", 0),
        # None means "this deployment does not verify", which is a different fact from
        # 'skipped' and is kept distinct rather than defaulted away.
        "verification_verdict": verification.get("verdict"),
        "verification_rounds": verification.get("rounds"),
        "verification_ms": verification.get("verification_ms"),
        "request_id": metadata.get("request_id"),
    }


def write_markdown(results: list[dict], summary: dict, path: Path) -> None:
    lines = [
        "# Evaluation results",
        "",
        f"Generated by `scripts/eval_run.py` against the deployed API. "
        f"{summary['count']} questions, "
        f"{summary['total_cost']:.4f} USD, median latency {summary['median_latency']:.0f} ms.",
        "",
        "`Behaviour` is whether the system did the right *kind* of thing: answered when it",
        "should answer, abstained when it should abstain. It says nothing about whether the",
        "answer was factually right -- that column is filled in by hand.",
        "",
        "| # | Question | Expected | Behaviour | Top doc correct | Top score | Confidence | Grounding | Cited |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        behaviour = "PASS" if r["behaviour_ok"] else "**FAIL**"
        if r["expect"] == "abstain":
            top_doc = "n/a"
        elif r["expected_ranked_first"]:
            top_doc = "yes"
        elif r["expected_retrieved"]:
            top_doc = "retrieved, not first"
        else:
            top_doc = "**no**"
        lines.append(
            f"| {r['id']} | {r['question'][:58]} | {r['expect']} | {behaviour} | {top_doc} | "
            f"{r['top_score']:.3f} | {r['confidence']} | {r['grounding']} | "
            f"{r['cited_count']}/{r['source_count']} |"
        )

    lines += [
        "",
        "## Score distribution",
        "",
        "This is what the relevance floor has to be calibrated against.",
        "",
        "| Group | n | min top_score | median | max |",
        "|---|---|---|---|---|",
    ]
    for label, key in (("Should be answered", "answered"), ("Should abstain", "abstain")):
        group = [r["top_score"] for r in results if r["expect"] == key]
        if group:
            lines.append(
                f"| {label} | {len(group)} | {min(group):.3f} | "
                f"{statistics.median(group):.3f} | {max(group):.3f} |"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _display(path: Path) -> str:
    """Repo-relative when the file is under the repo; --out may point anywhere."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url")
    parser.add_argument("--token")
    parser.add_argument("--stack", default="kbagent-dev")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--questions", type=Path, default=QUESTIONS,
                        help="question set to run (default: evaluation/questions.json)")
    parser.add_argument("--style", choices=("standard", "simple"), default="standard",
                        help="answer register to request; what the server used is recorded per result")
    parser.add_argument("--out", type=Path, default=RESULTS_JSON,
                        help="where the JSON goes; the markdown table is written beside it")
    args = parser.parse_args()

    api_url, token = (args.api_url, args.token)
    if not (api_url and token):
        api_url, token = from_stack(args.stack, args.profile)

    cases = json.loads(args.questions.read_text(encoding="utf-8"))["questions"]
    results = []

    print(f"Running {len(cases)} questions against {api_url}\n")
    for case in cases:
        status, body, elapsed = ask(api_url, token, case["question"], args.top_k, args.style)
        record = evaluate(case, status, body)
        results.append(record)

        mark = "ok  " if record["behaviour_ok"] else "FAIL"
        print(
            f"  [{mark}] {record['id']:<4} top={record['top_score']:.3f} "
            f"conf={record['confidence']} {record['grounding']:<20} "
            f"{record['top_document'] or '(no sources)'}"
        )

    summary = {
        "count": len(results),
        "behaviour_pass": sum(1 for r in results if r["behaviour_ok"]),
        "total_cost": sum(r["cost_usd"] for r in results),
        "median_latency": statistics.median([r["latency_ms"] or 0 for r in results]),
        # So a file under evaluation/runs/ says on its own what produced it.
        "questions": args.questions.name,
        "style": args.style,
    }

    results_json, results_md = args.out, args.out.with_suffix(".md")
    results_json.parent.mkdir(parents=True, exist_ok=True)
    results_json.write_text(
        json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8"
    )
    write_markdown(results, summary, results_md)

    print(
        f"\n{summary['behaviour_pass']}/{summary['count']} behaved as expected  ·  "
        f"${summary['total_cost']:.4f}  ·  median {summary['median_latency']:.0f} ms"
    )
    print(f"Wrote {_display(results_json)} and {_display(results_md)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
