# Evidence: authentication

Captured against the deployed API. The token is redacted; everything else is verbatim.

The host below belongs to a stack that has since been torn down and rebuilt, so the URL will
not answer. It is left as captured rather than updated to the current one: rewriting a
transcript to match a later deployment would make it something other than evidence.
`python scripts/smoke_test.py --stack <name>` reproduces all three rejections on any
deployment.

## 1. No token — rejected before any Lambda runs

```console
$ curl -i https://p47ll8ivl9.execute-api.us-east-1.amazonaws.com/dev/health
HTTP/1.1 401 Unauthorized
{"error":"unauthorized","message":"Missing or malformed authorization token.","request_id":"0c5b9432-abe9-43ae-9772-a397a06e9fdd"}```

## 2. Malformed token — rejected by the gateway regex, still no Lambda invoked

```console
$ curl -i -H 'Authorization: Bearer short' https://p47ll8ivl9.execute-api.us-east-1.amazonaws.com/dev/health
HTTP/1.1 401 Unauthorized
{"error":"unauthorized","message":"Missing or malformed authorization token.","request_id":"5f26fd6c-4729-4362-8440-4ee5afa199cb"}```

## 3. Well-formed but wrong token — 403, distinct from 401 on purpose

```console
$ curl -i -H 'Authorization: Bearer AAAA...' https://p47ll8ivl9.execute-api.us-east-1.amazonaws.com/dev/health
HTTP/1.1 403 Forbidden
{"error":"forbidden","message":"The provided token is not valid.","request_id":"d5e11e9e-48e9-42e5-80d8-f52c229be2bc"}```

## 4. Correct token

```console
$ curl -H 'Authorization: Bearer <token>' https://p47ll8ivl9.execute-api.us-east-1.amazonaws.com/dev/health
{
    "status": "ok",
    "kb_loaded": true,
    "version": "1.0.0",
    "provider": "openrouter",
    "model": "anthropic/claude-haiku-4.5",
    "embedding_model": "openai/text-embedding-3-small",
    "kb_version": "2026-09-08T14:40:00+00:00",
    "chunk_count": 52,
    "document_count": 8,
    "dimensions": 512,
    "index": {
        "loaded": true,
        "load_ms": 120.9,
        "etag": "\"469c4e5b326c86277b7f265b3926ff1d\""
    }
}
```

Every rejection carries the documented envelope **including `request_id`**. That is the
payoff of choosing REST API over HTTP API (ADR-03): Gateway Responses do not exist on
HTTP API, which returns a bare `{"message":"Forbidden"}` with no way to change it.
