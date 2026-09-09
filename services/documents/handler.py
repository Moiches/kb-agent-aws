"""Documents Lambda: list what is in the knowledge base, and remove things from it.

Separate from the query Lambda on purpose. That one is granted read on `index/*` and nothing
else, under a rule written into the stack: *a query path that cannot corrupt the knowledge
base is one less thing to reason about*. Deleting documents needs write access to `raw/`, so
it goes behind its own function and its own role rather than widening the one that answers
questions. The blast radius of a bug in either stays where it started.

Two ideas shape the API:

*   **`GET /documents` reports two realities, not one.** What is stored in `raw/` and what is
    actually searchable are different things, and they drift: uploading a file to S3 indexes
    nothing until the ingest runs. That gap is invisible from `/health`, which only ever knew
    about the index. Here it is named -- `pending_ingest` for uploaded-but-not-indexed,
    `orphaned_in_index` for the reverse -- so "I added a document and nothing happened" is a
    question the API answers instead of a mystery.

*   **`DELETE` returns 202, not 200.** Not primarily because of the 29 seconds API Gateway
    allows an integration -- rebuilding this corpus measures 1 to 11 seconds, so a
    synchronous reindex would fit today and stop fitting a few hundred documents from now.
    The better reason is that finishing the reindex is not the same as the deletion taking
    effect: the query Lambda holds the index in memory and re-checks it at most once every
    `INDEX_REFRESH_SECONDS`. Measured end to end, the artifact was rebuilt in 6 seconds and
    the query path answered from the old one for 54 more. A 200 would claim something that
    is not true yet for most of a minute.

There is no upload endpoint. API Gateway caps a request body at 10 MB, which a real document
set outgrows immediately, so uploads belong on a presigned S3 URL -- a different design, not
a bigger version of this one.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.parse

import boto3

BUCKET = os.environ.get("KB_BUCKET", "")
RAW_PREFIX = os.environ.get("RAW_PREFIX", "raw/")
INDEX_KEY = os.environ.get("KB_INDEX_KEY", "index/kb-index.json.gz")
INGEST_FUNCTION_NAME = os.environ.get("INGEST_FUNCTION_NAME", "")
SUPPORTED_EXTENSIONS = (".md", ".markdown", ".txt", ".pdf")

# Enforced by S3 itself through a presigned-POST condition, not by the client. A limit the
# uploader can edit is not a limit. 20 MB is set by what the corpus can afford rather than by
# what S3 can hold: every uploaded byte becomes chunks, chunks become embeddings, embeddings
# are billed, and the ingest refuses outright past MAX_INDEX_CHUNKS.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
UPLOAD_URL_TTL_SECONDS = int(os.environ.get("UPLOAD_URL_TTL_SECONDS", "300"))

_s3 = boto3.client("s3")
_lambda = boto3.client("lambda")

# The index summary, keyed by the ETag it was derived from. The artifact is ~0.5 MB and this
# endpoint only needs per-document chunk counts out of it, so recomputing that on every call
# would be paying to learn something that has not changed.
_summary_cache: tuple[str, dict] | None = None


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


def log(message: str, **fields) -> None:
    print(json.dumps({"message": message, **fields}))


def lambda_handler(event, context):  # noqa: ARG001 -- signature fixed by Lambda
    request_id = _request_id(event)
    method = (event.get("httpMethod") or "").upper()
    resource = event.get("resource") or event.get("path") or ""

    try:
        if not BUCKET:
            raise ApiError(500, "internal_error", "KB_BUCKET is not configured.")
        if method == "GET" and resource.endswith("/documents"):
            return _respond(200, list_documents(), request_id)
        if method == "POST" and resource.endswith("/documents"):
            return _respond(200, create_upload(event, request_id), request_id)
        if method == "DELETE" and resource.endswith("{documentId}"):
            body, status = delete_document(event, request_id)
            return _respond(status, body, request_id)
        raise ApiError(404, "not_found", f"No route for {method} {resource}")
    except ApiError as exc:
        log("request_failed", error=exc.error, status=exc.status, detail=exc.message,
            request_id=request_id)
        return _respond(exc.status,
                        {"error": exc.error, "message": exc.message, "request_id": request_id},
                        request_id)
    except Exception as exc:  # noqa: BLE001
        # The detail goes to the log, keyed by request id. It does not go to the caller.
        log("unhandled_exception", error=type(exc).__name__, detail=str(exc),
            request_id=request_id)
        return _respond(500, {"error": "internal_error",
                              "message": "Unexpected error. The request id identifies it in the logs.",
                              "request_id": request_id}, request_id)


# ------------------------------------------------------------------------------- GET


def list_documents() -> dict:
    stored = _stored_documents()
    indexed = _index_summary()

    stored_ids = {d["document_id"] for d in stored}
    indexed_ids = set(indexed["chunks_by_document"])

    documents = [
        {
            **d,
            "indexed": d["document_id"] in indexed_ids,
            "chunks": indexed["chunks_by_document"].get(d["document_id"], 0),
        }
        for d in stored
    ]
    pending = sorted(stored_ids - indexed_ids)
    orphaned = sorted(indexed_ids - stored_ids)

    return {
        "documents": documents,
        "index": {
            "kb_version": indexed["kb_version"],
            "chunk_count": indexed["chunk_count"],
            "document_count": len(indexed_ids),
            # The single field worth reading first: false means the searchable corpus is not
            # the stored corpus, and `npm run seed` is what closes the gap.
            "in_sync": not pending and not orphaned,
            "pending_ingest": pending,
            "orphaned_in_index": orphaned,
        },
    }


def _stored_documents() -> list[dict]:
    documents: list[dict] = []
    paginator = _s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=RAW_PREFIX):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith("/"):
                continue
            document_id = key[len(RAW_PREFIX):]
            if not document_id:
                continue
            documents.append({
                "document_id": document_id,
                "size_bytes": item["Size"],
                "last_modified": item["LastModified"].isoformat(),
                # Reported rather than filtered out: a file the ingest will skip is exactly
                # the kind of thing someone needs to be told about, not hidden from.
                "supported": document_id.lower().endswith(SUPPORTED_EXTENSIONS),
            })
    return sorted(documents, key=lambda d: d["document_id"])


def _index_summary() -> dict:
    """Per-document chunk counts, recomputed only when the artifact's ETag changes."""
    global _summary_cache

    try:
        etag = _s3.head_object(Bucket=BUCKET, Key=INDEX_KEY).get("ETag", "")
    except Exception:  # noqa: BLE001 -- no index yet is a state, not an error
        return {"kb_version": None, "chunk_count": 0, "chunks_by_document": {}}

    if _summary_cache and _summary_cache[0] == etag:
        return _summary_cache[1]

    payload = json.loads(gzip.decompress(
        _s3.get_object(Bucket=BUCKET, Key=INDEX_KEY)["Body"].read()
    ))
    counts: dict[str, int] = {}
    for chunk in payload.get("chunks", []):
        counts[chunk["document_id"]] = counts.get(chunk["document_id"], 0) + 1

    summary = {
        "kb_version": payload.get("kb_version"),
        "chunk_count": len(payload.get("chunks", [])),
        "chunks_by_document": counts,
    }
    _summary_cache = (etag, summary)
    return summary


# ---------------------------------------------------------------------------- UPLOAD


def create_upload(event: dict, request_id: str) -> dict:
    """Hand back a presigned POST the caller uploads to directly.

    The file never passes through this Lambda, and that is the point rather than an
    optimisation: API Gateway caps a request body at 10 MB, so any design that proxies the
    bytes has a ceiling a single real document can exceed. S3 takes the upload itself, and
    the conditions attached below are enforced by S3 -- the caller cannot raise its own
    size limit or redirect the object to another key.

    Uploading is all this grants. The event S3 emits afterwards is what triggers the
    reindex, so a document becomes searchable without anyone running a script.
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        raise ApiError(400, "bad_request", "Body must be JSON.") from None

    filename = _safe_filename(str(body.get("filename") or ""))
    replaces = filename in {d["document_id"] for d in _stored_documents()}

    presigned = _s3.generate_presigned_post(
        Bucket=BUCKET,
        Key=f"{RAW_PREFIX}{filename}",
        Fields={"Content-Type": _content_type(filename)},
        Conditions=[
            {"Content-Type": _content_type(filename)},
            # The reason this is a POST and not a PUT: a presigned PUT cannot bound the
            # body, so the limit would live in the client, where it is a suggestion.
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )
    log("upload_authorized", document_id=filename, replaces=replaces, request_id=request_id)

    return {
        "document_id": filename,
        "replaces_existing": replaces,
        "upload": {"url": presigned["url"], "fields": presigned["fields"], "method": "POST"},
        "max_bytes": MAX_UPLOAD_BYTES,
        "expires_in": UPLOAD_URL_TTL_SECONDS,
        "message": (
            "POST the file to `upload.url` as multipart/form-data: send every key in "
            "`upload.fields` first, then the file last under the field name `file`. "
            "Reindexing starts automatically once S3 has the object, and takes about a "
            "minute to affect answers."
        ),
        "request_id": request_id,
    }


def _safe_filename(raw: str) -> str:
    """Reject anything that is not a plain filename with a type the ingest can read."""
    filename = urllib.parse.unquote(raw).strip().replace("\\", "/")
    if not filename:
        raise ApiError(400, "bad_request", "A 'filename' is required.")
    # The presigned key is built by concatenation, unlike the delete path which resolves
    # against a listing -- there is nothing to resolve against for a file that does not
    # exist yet. So the validation here has to carry the whole weight.
    if "/" in filename or filename.startswith("."):
        raise ApiError(400, "bad_request",
                       "'filename' must be a plain file name: no directories, no leading dot.")
    if len(filename) > 200:
        raise ApiError(400, "bad_request", "'filename' is too long (200 characters maximum).")
    if not filename.lower().endswith(SUPPORTED_EXTENSIONS):
        raise ApiError(
            400, "bad_request",
            f"Unsupported file type. The ingest reads {', '.join(SUPPORTED_EXTENSIONS)}. "
            "Uploading anything else would store a document that never becomes searchable.",
        )
    return filename


def _content_type(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith((".md", ".markdown")):
        return "text/markdown"
    return "text/plain"


# ---------------------------------------------------------------------------- DELETE


def delete_document(event: dict, request_id: str) -> tuple[dict, int]:
    document_id = _requested_document_id(event)

    # Resolve against the listing rather than trusting the path. Building a key by
    # concatenation would make `..%2Findex%2Fkb-index.json.gz` a way to delete the index
    # itself; matching an id that S3 actually returned makes that class of input impossible
    # to express, instead of merely filtered.
    stored = {d["document_id"] for d in _stored_documents()}
    if document_id not in stored:
        raise ApiError(404, "not_found",
                       f"No document {document_id!r} in the knowledge base. "
                       "GET /documents lists what is there.")

    _s3.delete_object(Bucket=BUCKET, Key=f"{RAW_PREFIX}{document_id}")
    log("document_deleted", document_id=document_id, request_id=request_id)

    reindex = _start_reindex(request_id)
    remaining = sorted(stored - {document_id})
    return {
        "deleted": document_id,
        "remaining_documents": len(remaining),
        "reindex": reindex,
        "message": (
            "Document deleted. Reindexing was started in the background; until it finishes "
            "the deleted document is still searchable. Poll GET /documents and wait for "
            "index.in_sync."
            if reindex == "started" else
            "Document deleted, but reindexing could not be started. Run the seed script to "
            "rebuild the index; until then the deleted document is still searchable."
        ),
        "request_id": request_id,
    }, 202


def _requested_document_id(event: dict) -> str:
    raw = (event.get("pathParameters") or {}).get("documentId") or ""
    # API Gateway decodes path parameters, but a client that double-encodes a filename with
    # spaces is common enough to be worth surviving.
    document_id = urllib.parse.unquote(raw).strip()
    if not document_id:
        raise ApiError(400, "bad_request", "A document id is required.")
    return document_id


def _start_reindex(request_id: str) -> str:
    """Fire the ingest and do not wait. Re-embedding the corpus outlasts the 29 seconds
    API Gateway allows an integration, so the alternative to asynchronous is a timeout."""
    if not INGEST_FUNCTION_NAME:
        return "unavailable"
    try:
        _lambda.invoke(
            FunctionName=INGEST_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps({"reason": "document_deleted", "request_id": request_id}).encode(),
        )
        return "started"
    except Exception as exc:  # noqa: BLE001
        # The document is already gone; failing the whole call now would report a delete that
        # did happen as one that did not. Say what is true: deleted, not yet reindexed.
        log("reindex_failed", error=type(exc).__name__, detail=str(exc), request_id=request_id)
        return "failed"


# ------------------------------------------------------------------------------ shared


def _request_id(event: dict) -> str:
    return (event.get("requestContext") or {}).get("requestId") or f"local-{int(time.time())}"


def _respond(status: int, body: dict, request_id: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "x-request-id": request_id},
        "body": json.dumps(body),
    }
