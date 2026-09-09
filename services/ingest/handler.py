"""Ingest Lambda: build the knowledge base index from the documents in S3.

Runs once, at deploy time, via a CDK TriggerFunction, and on demand afterwards. It reads
every document under `raw/`, chunks it, embeds the chunks, and writes a single gzipped JSON
artifact to `index/`. That artifact is the entire retrieval layer (ADR-01).

Document ingestion as a product feature is intentionally out of scope, which the brief
allows. This is what replaces it: the knowledge base is pre-seeded from version-controlled
documents so that a deployment produces a working system rather than an empty one.

**The first deploy cannot seed.** CDK creates the provider API key secret empty, and this
function runs in the same deployment, so on a first `cdk deploy` there is no key yet. Rather
than fail the trigger and roll the whole stack back, it exits cleanly and reports what to do
next. `GET /health` then reports `kb_loaded: false`, which is a state the API already
distinguishes from being down.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import sys
from pathlib import Path

# Vendored pure-Python dependencies (pypdf), installed by scripts/build_lambda_deps.py.
sys.path.insert(0, str(Path(__file__).parent / "vendor"))

import boto3  # noqa: E402  (available in the Lambda runtime)

from embedder import DEFAULT_BATCH_SIZE, EmbeddingError, OpenRouterEmbedder  # noqa: E402
from loaders import SUPPORTED_EXTENSIONS, UnsupportedDocumentError, load_document  # noqa: E402
from splitter import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, chunk_segments  # noqa: E402

BUCKET = os.environ.get("KB_BUCKET", "")
RAW_PREFIX = os.environ.get("RAW_PREFIX", "raw/")
INDEX_KEY = os.environ.get("KB_INDEX_KEY", "index/kb-index.json.gz")
SECRET_ARN = os.environ.get("PROVIDER_API_KEY_SECRET_ARN", "")

EMBED_MODEL = os.environ.get("EMBED_MODEL_ID", "openai/text-embedding-3-small")
EMBED_DIMENSIONS = int(os.environ.get("EMBED_DIMENSIONS", "512"))
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP)))

# A ceiling on the whole corpus, checked before a single embedding is bought.
#
# Two things break past it and neither breaks gracefully. Embedding is billed per token, so
# an accidental upload -- a 200 MB PDF, a directory dropped into the bucket -- turns into a
# real charge against a $20 budget without anyone approving it. And the query Lambda holds
# the parsed index in memory at 1024 MB; measured at 226 chunks the artifact is 511 KiB, so
# this ceiling keeps it near 5 MB and the in-memory form far from the limit.
#
# Refusing leaves the previous index in place. A knowledge base that is out of date is
# recoverable; one that was half-written, or a budget that is spent, is not.
MAX_INDEX_CHUNKS = int(os.environ.get("MAX_INDEX_CHUNKS", "2500"))

INDEX_SCHEMA_VERSION = 1

# Embeddings arrive as float32 from the provider -- about 7 significant decimal digits --
# but Python serializes them with 17, so the artifact stores precision the source data never
# had. Rounding to 7 halves the compressed index (251 KiB -> 113 KiB measured) and shifts a
# dot product by at most 8e-8, which is three orders of magnitude below the provider's own
# float32 noise. Smaller index means a faster cold start, and cold start is the only place
# the index size is ever felt.
EMBEDDING_DECIMALS = 7

# Shared contract with infra/lib/kb-agent-stack.ts. CDK creates the secret holding this
# marker so that "nobody has pasted the key yet" is a state we can detect and report,
# rather than a random generated string that reaches the provider and 401s mid-deploy.
PROVIDER_KEY_PLACEHOLDER = "REPLACE_WITH_PROVIDER_API_KEY"

s3 = boto3.client("s3")


def log(message: str, **fields) -> None:
    """Structured logging without a dependency; Powertools arrives with the query Lambda."""
    print(json.dumps({"message": message, **fields}, default=str))


def lambda_handler(event, context):  # noqa: ARG001  (signature fixed by Lambda)
    if not BUCKET:
        raise RuntimeError("KB_BUCKET is not set")

    api_key = read_provider_key()
    if not api_key:
        # Deliberately a success. Failing here would roll back a deployment whose only
        # problem is that a human has not pasted a secret yet.
        log(
            "seeding_skipped",
            reason="provider api key secret is empty",
            action=f"aws secretsmanager put-secret-value --secret-id {SECRET_ARN} "
            "--secret-string 'sk-or-...' && make seed",
        )
        return {"seeded": False, "reason": "no_api_key"}

    documents = list_documents()
    if not documents:
        # An empty corpus is a real state, and it has to be written down. Returning early
        # would leave the previous index in place, so deleting the last document through
        # DELETE /documents would keep it searchable -- a deletion the system reports as
        # done and then quietly ignores. Publishing an empty index makes the removal true.
        log("seeding_empty", reason=f"no supported documents under s3://{BUCKET}/{RAW_PREFIX}")
        artifact = build_artifact([], document_count=0)
        write_index(artifact)
        return {"seeded": True, "documents": 0, "chunks": 0,
                "kb_version": artifact["kb_version"], "reason": "no_documents"}

    records = build_chunks(documents)
    if not records:
        log("seeding_skipped", reason="documents produced no chunks")
        return {"seeded": False, "reason": "no_chunks"}

    if len(records) > MAX_INDEX_CHUNKS:
        # Loud, and before the money is spent. The index on S3 is untouched, so the system
        # keeps answering from the corpus it had rather than from a partial rebuild.
        log(
            "seeding_refused",
            reason="corpus exceeds MAX_INDEX_CHUNKS",
            chunks=len(records),
            limit=MAX_INDEX_CHUNKS,
            documents=len(documents),
            action="remove a document, or raise MAX_INDEX_CHUNKS knowing it costs embeddings "
                   "and query Lambda memory",
        )
        return {"seeded": False, "reason": "too_many_chunks",
                "chunks": len(records), "limit": MAX_INDEX_CHUNKS}

    embedder = OpenRouterEmbedder(
        api_key=api_key,
        model=EMBED_MODEL,
        dimensions=EMBED_DIMENSIONS,
        batch_size=EMBED_BATCH_SIZE,
    )
    try:
        vectors = embedder.embed_batch([record["text"] for record in records])
    except EmbeddingError as exc:
        # This one *should* fail loudly: the key exists but the provider rejected us, and a
        # half-built index is worse than none.
        log("embedding_failed", error=str(exc))
        raise

    for record, vector in zip(records, vectors):
        record["embedding"] = [round(component, EMBEDDING_DECIMALS) for component in vector]

    artifact = build_artifact(records, document_count=len(documents))
    size = write_index(artifact)

    log(
        "seeding_complete",
        documents=len(documents),
        chunks=len(records),
        dimensions=EMBED_DIMENSIONS,
        index_key=INDEX_KEY,
        compressed_bytes=size,
        kb_version=artifact["kb_version"],
    )
    return {
        "seeded": True,
        "documents": len(documents),
        "chunks": len(records),
        "kb_version": artifact["kb_version"],
    }


def build_artifact(records: list[dict], *, document_count: int) -> dict:
    """The index artifact. Also the contract between the two Lambdas -- they share this
    JSON shape, not code, which is what keeps them independently deployable."""
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "kb_version": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "embedding_model": EMBED_MODEL,
        "dimensions": EMBED_DIMENSIONS,
        "normalized": True,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "document_count": document_count,
        "chunk_count": len(records),
        "chunks": records,
    }


def read_provider_key() -> str:
    """Fetch the provider API key. Absent or unset is a normal state, not an error."""
    if not SECRET_ARN:
        return ""
    try:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ARN)
    except Exception as exc:  # noqa: BLE001 -- any failure here means "not seeded yet"
        log("provider_key_unavailable", error=type(exc).__name__)
        return ""
    value = (secret.get("SecretString") or "").strip()
    return "" if value == PROVIDER_KEY_PLACEHOLDER else value


def list_documents() -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=RAW_PREFIX):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith("/"):
                continue
            if key.lower().endswith(SUPPORTED_EXTENSIONS):
                keys.append(key)
            else:
                log("document_skipped", key=key, reason="unsupported extension")
    return sorted(keys)


def build_chunks(keys: list[str]) -> list[dict]:
    """Load and chunk every document. One bad file must not sink the whole run."""
    records: list[dict] = []
    for key in keys:
        document_id = key[len(RAW_PREFIX) :] or key
        data = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        try:
            document = load_document(document_id, data)
        except (UnsupportedDocumentError, Exception) as exc:  # noqa: BLE001
            log("document_failed", document_id=document_id, error=f"{type(exc).__name__}: {exc}")
            continue

        full_text = "" if document_id.lower().endswith(".pdf") else data.decode("utf-8", errors="replace")
        chunks = chunk_segments(
            document_id,
            document.segments,
            full_text=full_text,
            document_title=document.title,
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
        )
        log(
            "document_chunked",
            document_id=document_id,
            title=document.title,
            segments=len(document.segments),
            chunks=len(chunks),
        )
        records.extend(chunks)
    return records


def write_index(artifact: dict) -> int:
    payload = gzip.compress(json.dumps(artifact).encode(), compresslevel=9)
    s3.put_object(
        Bucket=BUCKET,
        Key=INDEX_KEY,
        Body=payload,
        ContentType="application/json",
        ContentEncoding="gzip",
        Metadata={
            "kb-version": artifact["kb_version"],
            "chunk-count": str(artifact["chunk_count"]),
            "embedding-model": artifact["embedding_model"],
        },
    )
    return len(payload)
