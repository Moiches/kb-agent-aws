"""Run the evaluation set against the deployed API and record what happened.

    python scripts/eval_run.py --api-url ... --token ...
    python scripts/eval_run.py --stack kbagent-mc-dev        # reads both from AWS

Produces two artifacts:

  evaluation/results.json   every response in full, for calibration
  evaluation/results.md     the table that goes into EVALUATION.md

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
RESULTS_MD = ROOT / "evaluation" / "results.md"

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


def ask(api_url: str, token: str, question: str, top_k: int) -> tuple[int, dict, float]:
    payload = json.dumps({"question": question, "session_id": "evaluation", "top_k": top_k}).encode()
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
    abstained = body.get("metadata", {}).get("abstained", False)
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
        "components": body.get("metadata", {}).get("confidence_components", {}),
        "latency_ms": body.get("metadata", {}).get("latency_ms"),
        "input_tokens": body.get("metadata", {}).get("input_tokens", 0),
        "output_tokens": body.get("metadata", {}).get("output_tokens", 0),
        "cost_usd": body.get("metadata", {}).get("estimated_cost_usd", 0),
        "request_id": body.get("metadata", {}).get("request_id"),
    }


def write_markdown(results: list[dict], summary: dict) -> None:
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

    RESULTS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url")
    parser.add_argument("--token")
    parser.add_argument("--stack", default="kbagent-dev")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    api_url, token = (args.api_url, args.token)
    if not (api_url and token):
        api_url, token = from_stack(args.stack, args.profile)

    cases = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    results = []

    print(f"Running {len(cases)} questions against {api_url}\n")
    for case in cases:
        status, body, elapsed = ask(api_url, token, case["question"], args.top_k)
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
    }

    RESULTS_JSON.write_text(
        json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8"
    )
    write_markdown(results, summary)

    print(
        f"\n{summary['behaviour_pass']}/{summary['count']} behaved as expected  ·  "
        f"${summary['total_cost']:.4f}  ·  median {summary['median_latency']:.0f} ms"
    )
    print(f"Wrote {RESULTS_JSON.relative_to(ROOT)} and {RESULTS_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
