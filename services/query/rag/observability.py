"""Structured logging and CloudWatch metrics, with no third-party dependency.

The original plan used the AWS Lambda Powertools managed layer. It was dropped for two
reasons: the layer ARN is region and version specific, so a stale version number turns into
a deployment failure on someone else's account, and pulling in a layer contradicts the
zero-dependency promise the query Lambda makes elsewhere (ADR-02). What Powertools provides
here is a JSON logger and the EMF metric format, which are twenty lines each.

**Embedded Metric Format** is the trick worth knowing: CloudWatch extracts metrics from
specially-shaped log lines, so publishing a metric costs a `print()` rather than a
`PutMetricData` API call. No latency in the request path, no extra IAM permission.

Custom metrics bill at $0.30/metric/month, which is why `EMIT_METRICS=false` exists.
"""

from __future__ import annotations

import json
import os
import sys
import time

SERVICE = "kb-agent-query"
METRIC_NAMESPACE = "KbAgent"

# Set once per request by the handler and attached to every subsequent line, so one grep
# reconstructs a whole request. It is API Gateway's request id, which also appears in the
# access log, the X-Ray trace, the DynamoDB item and the JSON the caller receives.
_correlation_id: str | None = None


def set_correlation_id(request_id: str | None) -> None:
    global _correlation_id
    _correlation_id = request_id


def _xray_trace_id() -> str | None:
    """The X-Ray trace this invocation belongs to, from the runtime's own environment.

    Logging it is what actually connects the request id to a trace. X-Ray traces are keyed
    by their own id, not by API Gateway's request id, so without this line the claim that
    "one id spans five systems" would be false for one of the five. Reading the environment
    variable keeps that true without pulling in the X-Ray SDK and breaking the
    zero-dependency promise.
    """
    header = os.environ.get("_X_AMZN_TRACE_ID", "")
    for part in header.split(";"):
        if part.startswith("Root="):
            return part[5:]
    return None


def log(message: str, level: str = "INFO", **fields) -> None:
    record = {
        "level": level,
        "message": message,
        "service": SERVICE,
        "correlation_id": _correlation_id,
        "xray_trace_id": _xray_trace_id(),
        **fields,
    }
    # `default=str` so an unexpected type degrades to a string instead of throwing inside
    # the logger, which would turn an observability problem into an outage.
    print(json.dumps(record, default=str), file=sys.stdout)


def log_error(message: str, **fields) -> None:
    log(message, level="ERROR", **fields)


def emit_metrics(metrics: dict[str, float], dimensions: dict[str, str] | None = None) -> None:
    """Publish metrics via Embedded Metric Format.

    Silently does nothing when EMIT_METRICS is false: custom metrics are the single largest
    line in this project's AWS bill, so switching them off has to be free.
    """
    if os.environ.get("EMIT_METRICS", "true").lower() != "true":
        return
    if not metrics:
        return

    dimensions = dimensions or {}
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRIC_NAMESPACE,
                    "Dimensions": [list(dimensions.keys())] if dimensions else [[]],
                    "Metrics": [{"Name": name} for name in metrics],
                }
            ],
        },
        **dimensions,
        **metrics,
    }
    print(json.dumps(payload), file=sys.stdout)


class Timer:
    """Measures a stage of the request so latency can be attributed rather than guessed.

        with Timer() as t:
            ...
        t.ms
    """

    def __enter__(self) -> "Timer":
        self._started = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *args) -> bool:
        self.ms = round((time.perf_counter() - self._started) * 1000, 1)
        return False


def fingerprint(value: str) -> str:
    """Identify a secret in logs without disclosing it."""
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else ""
