"""Put evaluation runs side by side, question by question.

    python scripts/eval_compare.py evaluation/runs/M-r1.json evaluation/runs/L1-r1.json [more...]
    npm run eval:compare -- A.json B.json

The first file is the reference; every other file is read against it. Each question id gets
one row per run: behaviour, whether the expected document ranked first, the top score and
its delta from the reference, how many retrieved chunk_ids differ from the reference (the
size of the symmetric difference -- the indented lines under the row name them, -/+), then
confidence, grounding, cited/source, latency, cost, and the verification verdict and rounds
where the deployment produced any. The footer gives per run the behaviour pass count, the
summed cost, the median latency and the largest |delta top_score|.

The retrieval columns come before the answer columns on purpose. Two runs of the same
deployment should show a chunk difference of 0 on every row and a top-score delta within
embedding noise (about 1e-3). When they do not, retrieval moved, and nothing downstream of
it -- verdicts, cost, latency -- can be attributed to anything else until that is explained.

Stdlib only, like every script here. It reads what eval_run.py writes and nothing else.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

HEADER = (
    f"{'':<5}{'run':<22}{'beh':<5}{'first':<6}{'top':>7}{'delta':>8}{'chunks':>7}"
    f"{'conf':>6}  {'grounding':<21}{'cited':>6}{'lat_ms':>8}{'cost':>10}  {'verdict':<11}{'rounds':>6}"
)


def load(path: str) -> tuple[str, dict[str, dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Path(path).name, {r["id"]: r for r in data["results"]}


def fmt(value, spec: str = "") -> str:
    """None prints as '-' rather than crashing the table: files written before a column
    existed lack it, and main never produces the verification fields at all."""
    return "-" if value is None else format(value, spec)


def chunk_diff(reference: dict, record: dict) -> tuple[str, list[str]]:
    """Size of the symmetric difference of the retrieved chunk_ids, plus the ids themselves
    prefixed - (only in the reference) or + (only in this run). 'n/a' when either side
    predates the column, so an older baseline is never reported as 'every chunk changed'."""
    if "chunk_ids" not in reference or "chunk_ids" not in record:
        return "n/a", []
    before, after = set(reference["chunk_ids"]), set(record["chunk_ids"])
    ids = [f"-{c}" for c in reference["chunk_ids"] if c not in after]
    ids += [f"+{c}" for c in record["chunk_ids"] if c not in before]
    return str(len(ids)), ids


def row(qid: str, run: str, record: dict, reference: dict | None) -> list[str]:
    """The reference row itself, and a question the reference never asked, carry no delta."""
    delta = None if reference is None else record["top_score"] - reference["top_score"]
    chunks, ids = ("-", []) if reference is None else chunk_diff(reference, record)
    cited = f"{record['cited_count']}/{record['source_count']}"
    lines = [
        f"{qid:<5}{run[:21]:<22}{'ok' if record['behaviour_ok'] else 'FAIL':<5}"
        f"{'yes' if record['expected_ranked_first'] else 'no':<6}"
        f"{record['top_score']:>7.4f}{fmt(delta, '+.4f'):>8}{chunks:>7}"
        f"{fmt(record['confidence']):>6}  {fmt(record['grounding']):<21}{cited:>6}"
        f"{fmt(record['latency_ms'], '.0f'):>8}{fmt(record['cost_usd'], '.6f'):>10}  "
        f"{fmt(record.get('verification_verdict')):<11}{fmt(record.get('verification_rounds')):>6}"
    ]
    return lines + [f"{'':<27}{c}" for c in ids]


def footer(runs: list[tuple[str, dict[str, dict]]]) -> list[str]:
    _, reference = runs[0]
    lines = ["", f"{'run':<22}{'behaviour':>10}{'sum cost':>11}{'median lat_ms':>15}{'max |d top|':>13}"]
    for name, records in runs:
        values = list(records.values())
        passes = sum(1 for r in values if r["behaviour_ok"])
        cost = sum(r["cost_usd"] or 0 for r in values)
        latency = statistics.median([r["latency_ms"] or 0 for r in values]) if values else 0
        deltas = [abs(r["top_score"] - reference[q]["top_score"])
                  for q, r in records.items() if q in reference]
        max_delta = "-" if records is reference else f"{max(deltas, default=0):.4f}"
        lines.append(
            f"{name[:21]:<22}{f'{passes}/{len(values)}':>10}{cost:>11.6f}{latency:>15.0f}{max_delta:>13}"
        )
    return lines


def main(paths: list[str]) -> int:
    if not paths:
        print("usage: python scripts/eval_compare.py A.json B.json [more...]", file=sys.stderr)
        return 2
    runs = [load(p) for p in paths]
    _, reference = runs[0]
    # Question order follows the reference; ids only a later run asked are appended, so a
    # different question set in one file is visible rather than silently dropped.
    ids = list(dict.fromkeys(list(reference) + [q for _, r in runs[1:] for q in r]))

    print(HEADER)
    for qid in ids:
        for name, records in runs:
            if qid in records:
                ref = None if records is reference else reference.get(qid)
                print("\n".join(row(qid, name, records[qid], ref)))
    print("\n".join(footer(runs)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
