"""Query Lambda: the whole request path behind the authenticated API.

    GET  /health   liveness plus whether the knowledge base is actually loaded
    POST /query    question in, grounded answer with verified citations out

Zero third-party dependencies. Only `boto3` from the Lambda runtime and the standard
library, which is what keeps the deployment package small, the cold start short, and
`cdk deploy` free of Docker (ADR-02).

One request id threads through everything: API Gateway's `$context.requestId` becomes the
log correlation id, the DynamoDB sort key, and `metadata.request_id` in the response. Given
a request id from a user, one Logs Insights query reconstructs what happened.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid

import boto3

from rag import config, index_store
from rag.confidence import compute_confidence, grounding_label, verify_citations
from rag.models import ApiError, QueryRequest, Source
from rag.observability import Timer, emit_metrics, log, log_error, set_correlation_id
from rag.prompt import INSUFFICIENT_SENTINEL, build_user_message, system_prompt
from rag.providers import ErrorKind, ProviderError, get_provider
from rag.retriever import VectorStore

SERVICE_VERSION = config.SERVICE_VERSION

_secrets = boto3.client("secretsmanager")
_dynamodb = boto3.resource("dynamodb")

# Cached across warm invocations, with a TTL so a rotated key takes effect without a deploy.
_provider_key: str = ""
_provider_key_read_at: float = 0.0

# Provider failure kinds mapped onto the documented API error contract. The client's error
# handling must not change when the provider does.
_ERROR_STATUS: dict[ErrorKind, tuple[int, str]] = {
    ErrorKind.THROTTLED: (503, "model_throttled"),
    ErrorKind.TIMEOUT: (504, "model_timeout"),
    ErrorKind.ACCESS_DENIED: (500, "model_access_denied"),
    ErrorKind.BAD_REQUEST: (400, "bad_request"),
    ErrorKind.UNAVAILABLE: (503, "model_unavailable"),
    ErrorKind.INTERNAL: (500, "internal_error"),
}


def lambda_handler(event, context):  # noqa: ARG001
    request_id = _request_id(event)
    set_correlation_id(request_id)

    method = (event.get("httpMethod") or "").upper()
    path = event.get("path") or event.get("resource") or ""

    try:
        if method == "GET" and path.endswith("/health"):
            return _respond(200, health(), request_id)
        if method == "POST" and path.endswith("/query"):
            return _respond(200, query(event, request_id), request_id)
        raise ApiError(404, "not_found", f"No route for {method} {path}")
    except ProviderError as exc:
        api_error = provider_error_to_api_error(exc)
        log_error(
            "provider_failed", error=api_error.error, kind=exc.kind.value, detail=exc.message
        )
        return _respond(api_error.status, _error_body(api_error, request_id), request_id)
    except ApiError as exc:
        log_error("request_failed", error=exc.error, status=exc.status, detail=exc.message)
        return _respond(exc.status, _error_body(exc, request_id), request_id)
    except Exception as exc:  # noqa: BLE001
        # The stack trace goes to the log, keyed by request id. It never goes to the caller:
        # internal structure is not the client's business, and it is not theirs to debug.
        log_error("unhandled_exception", error=type(exc).__name__, detail=str(exc))
        return _respond(
            500,
            {"error": "internal_error", "message": "An unexpected error occurred.", "request_id": request_id},
            request_id,
        )


# ------------------------------------------------------------------------------- health


def health() -> dict:
    """Liveness, and whether there is anything to answer questions with.

    'Up but not seeded' is a real state -- it exists between `cdk deploy` and `make seed` --
    and reporting it as `degraded` rather than `ok` or an error is the difference between a
    reviewer knowing what to do next and filing a bug.
    """
    body = {
        "status": "ok",
        "kb_loaded": False,
        "version": SERVICE_VERSION,
        "provider": config.MODEL_PROVIDER,
        "model": config.MODEL_ID,
        "embedding_model": config.EMBED_MODEL_ID,
    }
    try:
        kb = index_store.load(config.KB_BUCKET, config.KB_INDEX_KEY)
    except index_store.IndexUnavailable:
        body["status"] = "degraded"
        body["detail"] = "knowledge base not seeded yet; run the seed command from the stack outputs"
        return body

    body.update(
        kb_loaded=True,
        kb_version=kb.kb_version,
        chunk_count=kb.chunk_count,
        document_count=kb.document_count,
        dimensions=kb.dimensions,
        index=index_store.stats(),
    )
    return body


# -------------------------------------------------------------------------------- query


def query(event: dict, request_id: str) -> dict:
    request = _parse_request(event)

    try:
        kb = index_store.load(config.KB_BUCKET, config.KB_INDEX_KEY)
    except index_store.IndexUnavailable:
        raise ApiError(
            503,
            "kb_unavailable",
            "The knowledge base has not been seeded yet.",
            hint="Run the seed command from the stack outputs, then retry.",
        ) from None

    provider = get_provider(
        config.MODEL_PROVIDER,
        _read_provider_key(),
        config.MODEL_ID,
        config.EMBED_MODEL_ID,
        config.EMBED_DIMENSIONS,
    )

    with Timer() as retrieval_timer:
        question_vector = provider.embed(request.question)
        hits = VectorStore(kb.chunks).search(
            question_vector,
            top_k=request.top_k,
            max_per_document=config.MAX_CHUNKS_PER_DOC,
        )

    scores = [hit.score for hit in hits]
    top_score = scores[0] if scores else 0.0

    # Short circuit. Nothing similar enough was found, so there is no point paying for a
    # generation that would either hedge or invent. Saves the call and the latency.
    if top_score < config.RELEVANCE_FLOOR:
        return _abstention(
            request, request_id, provider, kb, hits, scores,
            reason="top_score_below_floor",
            retrieval_ms=retrieval_timer.ms,
        )

    with Timer() as generation_timer:
        result = provider.generate(
            system=system_prompt(request.style),
            user=build_user_message(request.question, hits),
            max_tokens=config.MAX_OUTPUT_TOKENS,
            temperature=config.TEMPERATURE,
        )

    retrieved_ids = [hit.chunk.chunk_id for hit in hits]
    answer, cited, dropped = verify_citations(result.text, set(retrieved_ids))

    if answer.startswith(INSUFFICIENT_SENTINEL):
        return _abstention(
            request, request_id, provider, kb, hits, scores,
            reason="model_reported_insufficient_context",
            retrieval_ms=retrieval_timer.ms,
            generation_ms=generation_timer.ms,
            result=result,
            answer=answer[len(INSUFFICIENT_SENTINEL) :].strip() or None,
        )

    confidence, components = compute_confidence(
        scores, cited, retrieved_ids, floor=config.RELEVANCE_FLOOR, ceil=config.CONFIDENCE_CEIL
    )
    grounding = grounding_label(confidence, top_score, floor=config.RELEVANCE_FLOOR, abstained=False)
    sources = _sources(hits, cited)

    response = {
        "answer": answer,
        "confidence": confidence,
        "grounding": grounding,
        "sources": [source.to_dict() for source in sources],
        "metadata": _metadata(
            request, request_id, provider, kb, result=result,
            retrieval_ms=retrieval_timer.ms, generation_ms=generation_timer.ms,
            chunks_searched=kb.chunk_count,
            confidence_components=components,
            dropped_citations=dropped,
            top_score=top_score,
        ),
    }

    _record(request, response, top_score)
    return response


def _abstention(request, request_id, provider, kb, hits, scores, *, reason,
                retrieval_ms, generation_ms=0.0, result=None, answer=None) -> dict:
    """A documented, first-class outcome -- not an error and not a hedge.

    Whether the passages are returned depends on *which* abstention this is, because the two
    carry different information:

    *   `top_score_below_floor` -- nothing scored high enough to be worth reading. Returning
        the near misses would put irrelevant documents next to "I could not find this" and
        invite the reader to believe the answer came from them. Sources stay empty.

    *   `model_reported_insufficient_context` -- passages *were* retrieved and the model
        judged them not to answer the question. Here the retrieval is the useful part of the
        answer: it usually shows the right document was found and the wrong section of it,
        which is a very different problem from the document being absent. Withholding them
        was also inconsistent, since the model's own explanation quotes what it read while
        the response claimed there was nothing to show.

    In both cases `cited` is false for every passage: nothing was cited, because nothing was
    answered from. The `grounding: "insufficient_context"` label is what tells a client to
    present these as rejected evidence rather than as support.
    """
    top_score = scores[0] if scores else 0.0
    confidence, components = compute_confidence(
        scores, [], [hit.chunk.chunk_id for hit in hits],
        floor=config.RELEVANCE_FLOOR, ceil=config.CONFIDENCE_CEIL,
    )
    response = {
        "answer": answer or "I could not find information about that in the knowledge base.",
        "confidence": confidence,
        "grounding": "insufficient_context",
        "sources": [
            source.to_dict()
            for source in (_sources(hits, []) if reason == "model_reported_insufficient_context" else [])
        ],
        "metadata": _metadata(
            request, request_id, provider, kb, result=result,
            retrieval_ms=retrieval_ms, generation_ms=generation_ms,
            chunks_searched=kb.chunk_count,
            confidence_components=components,
            dropped_citations=[],
            top_score=top_score,
            extra={"abstained": True, "abstain_reason": reason},
        ),
    }
    _record(request, response, top_score)
    return response


def _sources(hits, cited: list[str]) -> list[Source]:
    cited_set = set(cited)
    return [
        Source(
            document_id=hit.chunk.document_id,
            document_title=hit.chunk.document_title,
            chunk_id=hit.chunk.chunk_id,
            section=hit.chunk.section,
            page=hit.chunk.page,
            score=hit.score,
            excerpt=" ".join(hit.chunk.text.split())[: config.EXCERPT_CHARS],
            cited=hit.chunk.chunk_id in cited_set,
        )
        for hit in hits
    ]


def _metadata(request, request_id, provider, kb, *, result, retrieval_ms, generation_ms,
              chunks_searched, confidence_components, dropped_citations, top_score=0.0, extra=None) -> dict:
    input_tokens = result.input_tokens if result else 0
    output_tokens = result.output_tokens if result else 0
    metadata = {
        "provider": provider.name,
        "model": result.model if result else provider.generation_model,
        "embedding_model": provider.embedding_model,
        "retrieval_strategy": "in_memory_cosine_topk",
        # Recorded so an answer's register is auditable after the fact: a reader comparing two
        # answers to the same question needs to know which prompt produced each.
        "style": request.style,
        "request_id": request_id,
        "session_id": request.session_id,
        "kb_version": kb.kb_version,
        "chunks_searched": chunks_searched,
        # Exposed because an abstention returns no sources, and without it the score that
        # triggered the abstention is invisible -- which is exactly the number the relevance
        # floor has to be calibrated against.
        "top_score": round(top_score, 4),
        "retrieval_ms": retrieval_ms,
        "generation_ms": generation_ms,
        "latency_ms": round(retrieval_ms + generation_ms, 1),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(
            input_tokens / 1000 * config.COST_PER_1K_INPUT_TOKENS
            + output_tokens / 1000 * config.COST_PER_1K_OUTPUT_TOKENS,
            6,
        ),
        "confidence_components": confidence_components,
        "dropped_citations": dropped_citations,
    }
    metadata.update(extra or {})
    return metadata


# ------------------------------------------------------------------------------ plumbing


def _parse_request(event: dict) -> QueryRequest:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        raise ApiError(400, "bad_request", "Request body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "Request body must be a JSON object.")

    question = str(body.get("question") or "").strip()
    if len(question) < config.MIN_QUESTION_CHARS:
        raise ApiError(400, "bad_request", f"'question' must be at least {config.MIN_QUESTION_CHARS} characters.")
    if len(question) > config.MAX_QUESTION_CHARS:
        raise ApiError(400, "bad_request", f"'question' must be at most {config.MAX_QUESTION_CHARS} characters.")

    top_k = body.get("top_k", config.TOP_K_DEFAULT)
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", "'top_k' must be an integer.") from None
    if not 1 <= top_k <= config.TOP_K_MAX:
        raise ApiError(400, "bad_request", f"'top_k' must be between 1 and {config.TOP_K_MAX}.")

    style = str(body.get("style") or config.DEFAULT_STYLE).strip().lower()
    if style not in config.ALLOWED_STYLES:
        raise ApiError(
            400, "bad_request",
            f"'style' must be one of: {', '.join(config.ALLOWED_STYLES)}.",
        )

    session_id = str(body.get("session_id") or "")[:64] or f"anon-{uuid.uuid4()}"
    return QueryRequest(question=question, session_id=session_id, top_k=top_k, style=style)


def _read_provider_key() -> str:
    """Cached with a TTL, so rotating the secret takes effect without a redeploy."""
    global _provider_key, _provider_key_read_at
    import time

    if _provider_key and (time.time() - _provider_key_read_at) < config.PROVIDER_KEY_TTL_SECONDS:
        return _provider_key
    if not config.PROVIDER_API_KEY_SECRET_ARN:
        return ""

    value = _secrets.get_secret_value(SecretId=config.PROVIDER_API_KEY_SECRET_ARN)
    _provider_key = (value.get("SecretString") or "").strip()
    _provider_key_read_at = time.time()
    return _provider_key


def _record(request: QueryRequest, response: dict, top_score: float) -> None:
    """Log the query, emit metrics, and persist it. None of it may break the response."""
    metadata = response["metadata"]
    fields = {
        "session_id": request.session_id,
        "top_score": round(top_score, 4),
        "confidence": response["confidence"],
        "grounding": response["grounding"],
        "style": request.style,
        "chunk_ids": [s["chunk_id"] for s in response["sources"]],
        "abstained": metadata.get("abstained", False),
        "provider": metadata["provider"],
        "model": metadata["model"],
        "input_tokens": metadata["input_tokens"],
        "output_tokens": metadata["output_tokens"],
        "estimated_cost_usd": metadata["estimated_cost_usd"],
        "retrieval_ms": metadata["retrieval_ms"],
        "generation_ms": metadata["generation_ms"],
        "dropped_citations": metadata["dropped_citations"],
    }
    if config.LOG_QUESTIONS:
        fields["question_preview"] = request.question[:200]
    log("query_completed", **fields)

    emit_metrics(
        {
            "QueryConfidence": response["confidence"],
            "Abstention": 1.0 if metadata.get("abstained") else 0.0,
            "ProviderInputTokens": metadata["input_tokens"],
            "ProviderOutputTokens": metadata["output_tokens"],
        },
        dimensions={"provider": metadata["provider"]},
    )

    if not config.QUERY_LOG_TABLE:
        return
    try:
        now = dt.datetime.now(dt.timezone.utc)
        _dynamodb.Table(config.QUERY_LOG_TABLE).put_item(
            Item={
                "session_id": request.session_id,
                "ts_request": f"{now.isoformat(timespec='milliseconds')}#{metadata['request_id']}",
                "question": request.question if config.LOG_QUESTIONS else "<redacted>",
                "grounding": response["grounding"],
                "confidence": json.dumps(response["confidence"]),
                "chunk_ids": [s["chunk_id"] for s in response["sources"]],
                "provider": metadata["provider"],
                "model": metadata["model"],
                "latency_ms": json.dumps(metadata["latency_ms"]),
                "expires_at": int(now.timestamp()) + config.QUERY_LOG_TTL_DAYS * 86400,
            }
        )
    except Exception as exc:  # noqa: BLE001
        # Logging is not worth an outage. The user already has their answer.
        log_error("query_log_write_failed", error=type(exc).__name__, detail=str(exc)[:200])


def _request_id(event: dict) -> str:
    return (event.get("requestContext") or {}).get("requestId") or str(uuid.uuid4())


def _error_body(exc: ApiError, request_id: str) -> dict:
    body = {"error": exc.error, "message": exc.message, "request_id": request_id}
    if exc.hint:
        body["hint"] = exc.hint
    return body


def _respond(status: int, body: dict, request_id: str) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "x-request-id": request_id,
        },
        "body": json.dumps(body),
    }


def provider_error_to_api_error(exc: ProviderError) -> ApiError:
    """Translate a provider failure into the documented API contract.

    This is the payoff of ADR-09's interface: the client sees `model_throttled` whether the
    provider raised an HTTP 429 or a boto3 ThrottlingException, so switching provider does
    not change the API's observable behaviour.
    """
    status, error = _ERROR_STATUS.get(exc.kind, (500, "internal_error"))
    return ApiError(status, error, exc.message, hint=exc.hint)
