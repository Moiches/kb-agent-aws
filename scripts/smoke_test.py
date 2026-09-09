"""Validate a deployed API against its published contract.

    python scripts/smoke_test.py --stack kbagent-mc-dev
    python scripts/smoke_test.py --api-url https://... --token ...

This is the script the brief asks for: it validates the query API contract, authentication
behaviour, and a successful sample query. Fourteen assertions, roughly two cents of model
calls, no AWS credentials needed if the URL and token are passed directly.

It is deliberately separate from `eval_run.py`. That one measures *answer quality* against a
question set and costs real money to run; this one measures whether the *deployment* is
wired correctly and is safe to run on every deploy. Confusing the two would mean either
paying for an evaluation to learn the API is up, or trusting a contract check to tell you
the answers are good.

Every assertion states what it expects and prints what it got, because a smoke test that
only says FAIL sends you back to the console to find out why.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

WINDOWS_CLI = Path(r"C:\Program Files\Amazon\AWSCLIV2\aws.exe")

# A question the sample knowledge base answers, and one it certainly does not.
IN_CORPUS = "What is the refund policy for enterprise customers?"
OUT_OF_CORPUS = "What is the airspeed velocity of an unladen swallow?"

# Well-formed enough to pass the gateway's token regex, wrong enough to fail the authorizer.
# The distinction is the point: it separates layer 1 (401) from layer 2 (403).
WELL_FORMED_BUT_WRONG = "z" * 48

# Fails the gateway regex `^Bearer [A-Za-z0-9_-]{32,}`, so no Lambda is ever invoked.
MALFORMED = "short"


class Failure(Exception):
    """One assertion did not hold."""


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


def call(api_url: str, path: str, token: str | None = None,
         payload: dict | None = None, method: str | None = None) -> tuple[int, dict]:
    """One HTTP call. Returns (status, parsed body) and never raises on an HTTP error --
    the error responses are exactly what several of these assertions are about."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method=method or ("POST" if payload is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": "client", "message": str(exc)}


def expect(condition: bool, description: str) -> None:
    if not condition:
        raise Failure(description)


def check_error_envelope(body: dict, error: str) -> None:
    """The brief asks for a consistent error shape. Gateway-produced errors are the ones
    most likely to drift, since they are configured rather than coded."""
    expect(body.get("error") == error, f"error should be {error!r}, got {body.get('error')!r}")
    expect(bool(body.get("message")), "error response carries no message")
    expect(bool(body.get("request_id")), "error response carries no request_id")


# --------------------------------------------------------------------------- assertions


def auth_rejects_a_missing_header(api: str, token: str) -> str:
    status, body = call(api, "/health")
    expect(status == 401, f"expected 401, got {status}")
    check_error_envelope(body, "unauthorized")
    return "401 unauthorized, envelope intact"


def auth_rejects_a_malformed_token(api: str, token: str) -> str:
    """Layer 1: the gateway's token regex. No Lambda is invoked, so this costs nothing."""
    status, body = call(api, "/health", token=MALFORMED)
    expect(status == 401, f"expected 401, got {status}")
    check_error_envelope(body, "unauthorized")
    return "401 at the gateway, authorizer never invoked"


def auth_rejects_a_wrong_token(api: str, token: str) -> str:
    """Layer 2: the authorizer's constant-time comparison."""
    status, body = call(api, "/health", token=WELL_FORMED_BUT_WRONG)
    expect(status == 403, f"expected 403, got {status}")
    check_error_envelope(body, "forbidden")
    return "403 forbidden, distinct from 401"


def health_reports_a_loaded_index(api: str, token: str) -> str:
    status, body = call(api, "/health", token=token)
    expect(status == 200, f"expected 200, got {status}: {body}")
    expect(body.get("kb_loaded") is True, f"kb_loaded is {body.get('kb_loaded')!r}")
    expect(body.get("chunk_count", 0) > 0, "chunk_count is 0 -- the index is empty")
    return f"200, {body['chunk_count']} chunks, kb_version {body.get('kb_version', '?')}"


def query_rejects_an_empty_question(api: str, token: str) -> str:
    status, body = call(api, "/query", token=token, payload={"question": ""})
    expect(status == 400, f"expected 400, got {status}")
    check_error_envelope(body, "bad_request")
    return "400 bad_request, rejected at the gateway"


def query_rejects_an_unknown_style(api: str, token: str) -> str:
    status, body = call(api, "/query", token=token,
                        payload={"question": IN_CORPUS, "style": "pirate"})
    expect(status == 400, f"expected 400, got {status}")
    check_error_envelope(body, "bad_request")
    return "400 bad_request, style enum enforced"


def query_answers_and_cites(api: str, token: str) -> str:
    status, body = call(api, "/query", token=token,
                        payload={"question": IN_CORPUS, "session_id": "smoke", "top_k": 5})
    expect(status == 200, f"expected 200, got {status}: {body}")
    expect(bool(body.get("sources")), "no sources returned for a question the corpus answers")
    confidence = body.get("confidence")
    expect(isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0,
           f"confidence out of range: {confidence!r}")
    expect(bool(body.get("metadata", {}).get("request_id")), "metadata carries no request_id")

    # Citation verification, checked from the outside: every id the answer cites must be a
    # passage that was actually retrieved. This is the property the whole grounding claim
    # rests on, so it is worth asserting against the live system, not only in unit tests.
    retrieved = {s["chunk_id"] for s in body["sources"]}
    cited = set(re.findall(r"\[([^\[\]]+?#chunk-\d+)\]", body.get("answer", "")))
    invented = cited - retrieved
    expect(not invented, f"answer cites passages that were not retrieved: {sorted(invented)}")
    return (f"200, confidence {confidence:.2f}, {len(cited)} citations, "
            f"all {len(retrieved)} sources verifiable")


def query_abstains_when_it_should(api: str, token: str) -> str:
    """The most important assertion here. A system that answers everything is easy; one
    that declines is what makes the other answers worth reading."""
    status, body = call(api, "/query", token=token,
                        payload={"question": OUT_OF_CORPUS, "session_id": "smoke"})
    expect(status == 200, f"expected 200, got {status}: {body}")
    expect(body.get("grounding") == "insufficient_context",
           f"grounding is {body.get('grounding')!r}, expected insufficient_context")

    # What `sources` should contain depends on which abstention path fired, so assert the
    # rule rather than one of its two outcomes -- otherwise this passes for the wrong reason.
    reason = body.get("metadata", {}).get("abstain_reason", "?")
    sources = body.get("sources", [])
    if reason == "top_score_below_floor":
        expect(sources == [], "nothing cleared the floor, yet passages were returned; "
                              "near misses beside 'I could not find this' invite the reader "
                              "to believe the answer came from them")
    else:
        expect(all(not s.get("cited") for s in sources),
               "an abstention marked a passage as cited")
    return f"abstained, reason {reason}, {len(sources)} passages shown"


def both_styles_stay_grounded(api: str, token: str) -> str:
    """The simple style must not buy readability with a lower evidence bar."""
    status, body = call(api, "/query", token=token,
                        payload={"question": IN_CORPUS, "style": "simple", "top_k": 5})
    expect(status == 200, f"expected 200, got {status}: {body}")
    expect(body.get("metadata", {}).get("style") == "simple", "style not echoed in metadata")
    retrieved = {s["chunk_id"] for s in body.get("sources", [])}
    cited = set(re.findall(r"\[([^\[\]]+?#chunk-\d+)\]", body.get("answer", "")))
    expect(bool(cited), "the simple style dropped its citations")
    expect(not (cited - retrieved), "the simple style cited passages that were not retrieved")
    return f"200, style echoed, {len(cited)} citations kept"


def documents_lists_stored_and_indexed_state(api: str, token: str) -> str:
    status, body = call(api, "/documents", token=token)
    expect(status == 200, f"expected 200, got {status}: {body}")
    expect(isinstance(body.get("documents"), list) and body["documents"],
           "no documents listed, but the knowledge base is seeded")
    index = body.get("index", {})
    for field in ("in_sync", "pending_ingest", "orphaned_in_index", "chunk_count"):
        expect(field in index, f"index summary is missing {field!r}")
    sync = "in sync" if index["in_sync"] else (
        f"OUT OF SYNC -- pending {index['pending_ingest']}, orphaned {index['orphaned_in_index']}")
    return f"200, {len(body['documents'])} documents, {index['chunk_count']} chunks, {sync}"


def deleting_something_absent_is_a_clean_404(api: str, token: str) -> str:
    """Non-destructive by construction: the id cannot exist."""
    status, body = call(api, "/documents/definitely-not-a-real-document.md",
                        token=token, method="DELETE")
    expect(status == 404, f"expected 404, got {status}: {body}")
    check_error_envelope(body, "not_found")
    return "404 not_found, envelope intact"


def a_document_id_cannot_escape_its_prefix(api: str, token: str) -> str:
    """The index sits one prefix away from the documents. If the key were built by
    concatenating the path parameter, this request would delete the knowledge base."""
    status, body = call(api, "/documents/..%2Findex%2Fkb-index.json.gz",
                        token=token, method="DELETE")
    expect(status in (400, 403, 404), f"traversal attempt returned {status}, expected a rejection")
    return f"{status}, traversal rejected"


def uploading_an_unreadable_type_is_refused(api: str, token: str) -> str:
    """Non-destructive: no presigned URL is issued, so nothing can be written. Storing a
    format the ingest cannot read would create a document that is visibly present and
    permanently unsearchable."""
    status, body = call(api, "/documents", token=token, payload={"filename": "notes.docx"})
    expect(status == 400, f"expected 400, got {status}: {body}")
    check_error_envelope(body, "bad_request")
    return "400 bad_request, unreadable type refused"


def an_upload_filename_cannot_escape_its_prefix(api: str, token: str) -> str:
    status, body = call(api, "/documents", token=token,
                        payload={"filename": "../index/kb-index.json.gz"})
    expect(status == 400, f"expected 400, got {status}: {body}")
    return "400, traversal refused before any URL is signed"


CHECKS = [
    ("auth: no header rejected", auth_rejects_a_missing_header),
    ("auth: malformed token rejected at the gateway", auth_rejects_a_malformed_token),
    ("auth: wrong token rejected by the authorizer", auth_rejects_a_wrong_token),
    ("health: index loaded", health_reports_a_loaded_index),
    ("contract: empty question rejected", query_rejects_an_empty_question),
    ("contract: unknown style rejected", query_rejects_an_unknown_style),
    ("query: answers and cites verifiably", query_answers_and_cites),
    ("query: abstains outside the corpus", query_abstains_when_it_should),
    ("query: simple style stays grounded", both_styles_stay_grounded),
    ("documents: listing reports drift between S3 and the index", documents_lists_stored_and_indexed_state),
    ("documents: deleting what is not there is a clean 404", deleting_something_absent_is_a_clean_404),
    ("documents: a path cannot escape the raw prefix", a_document_id_cannot_escape_its_prefix),
    ("upload: a type the ingest cannot read is refused", uploading_an_unreadable_type_is_refused),
    ("upload: a filename cannot escape the raw prefix", an_upload_filename_cannot_escape_its_prefix),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", help="Read the API URL and token from this stack's outputs.")
    parser.add_argument("--api-url")
    parser.add_argument("--token")
    parser.add_argument("--profile")
    args = parser.parse_args()

    if args.stack:
        api_url, token = from_stack(args.stack, args.profile)
    elif args.api_url and args.token:
        api_url, token = args.api_url, args.token
    else:
        return parser.error("pass --stack, or both --api-url and --token")

    print(f"Smoke test against {api_url}\n")
    failures = 0
    for name, check in CHECKS:
        try:
            print(f"  PASS  {name}\n          {check(api_url, token)}")
        except Failure as exc:
            failures += 1
            print(f"  FAIL  {name}\n          {exc}")

    passed = len(CHECKS) - failures
    print(f"\n{passed}/{len(CHECKS)} assertions passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
