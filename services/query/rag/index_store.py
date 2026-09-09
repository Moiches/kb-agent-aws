"""Loads the vector index from S3 and keeps it in memory.

The index is a single gzipped JSON artifact written by the ingest Lambda (ADR-01). This
module downloads it once per execution environment and holds it in a module-level global,
so only cold starts pay for it: the second request onwards, retrieval touches nothing but
local memory. That is why retrieval costs USD 0.00 and why there is no database.

A missing index is not an error, it is a state. The knowledge base is seeded after the
first deployment, so between `cdk deploy` and `make seed` there is a window where the API
is up and the knowledge base is empty. `GET /health` reports that as `degraded` rather than
pretending the service is broken.

Loading once per environment is not quite enough, though. Re-running the ingest writes a new
artifact, and a warm Lambda that never looks again keeps answering from the old one -- so a
document added to the knowledge base appears to have been ignored, with nothing in the logs
to say why. Silent staleness is the worst kind. Before serving a cached index this module
compares the object's ETag, at most once every `INDEX_REFRESH_SECONDS`, which costs one S3
HEAD per environment per minute and bounds how long a stale answer can survive.
"""

from __future__ import annotations

import gzip
import json
import time

import boto3

from . import config
from .models import KnowledgeBase

_s3 = boto3.client("s3")

# Survives across invocations of a warm Lambda. This is the entire caching strategy.
_cached_index: KnowledgeBase | None = None
_cached_etag: str | None = None
_load_ms: float = 0.0
_checked_at: float = 0.0


class IndexUnavailable(RuntimeError):
    """The index object does not exist yet -- the knowledge base has not been seeded."""


def load(bucket: str, key: str) -> KnowledgeBase:
    """Return the knowledge base, downloading it on a cold start or after a re-ingest."""
    global _cached_index, _cached_etag, _load_ms, _checked_at

    if _cached_index is not None and not _superseded(bucket, key):
        return _cached_index

    started = time.perf_counter()
    try:
        response = _s3.get_object(Bucket=bucket, Key=key)
    except _s3.exceptions.NoSuchKey:
        raise IndexUnavailable(f"no index at s3://{bucket}/{key}") from None
    except Exception as exc:  # noqa: BLE001
        # ClientError with a 404 shows up here too, depending on bucket permissions.
        if "NoSuchKey" in type(exc).__name__ or "404" in str(exc):
            raise IndexUnavailable(f"no index at s3://{bucket}/{key}") from None
        raise

    payload = json.loads(gzip.decompress(response["Body"].read()))
    _cached_index = KnowledgeBase.from_artifact(payload)
    _cached_etag = response.get("ETag")
    _load_ms = (time.perf_counter() - started) * 1000
    _checked_at = time.monotonic()
    return _cached_index


def _superseded(bucket: str, key: str) -> bool:
    """Has a re-ingest replaced the artifact we are holding?

    Rate-limited to one HEAD per `INDEX_REFRESH_SECONDS`, so the common case -- a warm
    environment answering back-to-back requests -- still touches nothing but memory.

    A failed check returns False deliberately. If S3 is unreachable, continuing to serve a
    possibly-stale index is better than failing a request that memory could have answered;
    the next check will pick up the change.
    """
    global _checked_at

    if config.INDEX_REFRESH_SECONDS <= 0:
        return False
    now = time.monotonic()
    if now - _checked_at < config.INDEX_REFRESH_SECONDS:
        return False
    _checked_at = now

    try:
        head = _s3.head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001
        return False
    return head.get("ETag") != _cached_etag


def peek() -> KnowledgeBase | None:
    """The cached index, or None if this environment has not loaded one yet."""
    return _cached_index


def stats() -> dict:
    """Cache diagnostics, surfaced in `GET /health` and in the query log."""
    return {
        "loaded": _cached_index is not None,
        "load_ms": round(_load_ms, 1),
        "etag": _cached_etag,
    }


def reset() -> None:
    """Drop the cache. For tests, and for a future cache-busting admin call."""
    global _cached_index, _cached_etag, _load_ms, _checked_at
    _cached_index = None
    _cached_etag = None
    _load_ms = 0.0
    _checked_at = 0.0
