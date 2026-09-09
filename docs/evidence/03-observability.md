# Evidence: one request id across five systems

The same request, `a5e192c4-f3dd-4b0c-ad42-4d55d99b50c4`, followed through everything that
recorded it. This is what makes a user's bug report actionable: they quote one id, and the
whole path is reconstructible.

## 1. The response the caller received

```json
"metadata": {
  "request_id": "a5e192c4-f3dd-4b0c-ad42-4d55d99b50c4",
  "confidence": 0.76,
  "grounding": "high"
}
```

## 2. API Gateway access log

`/aws/apigateway/kbagent-mc-dev`

```json
{"requestId":"a5e192c4-f3dd-4b0c-ad42-4d55d99b50c4","ip":"189.217.223.128",
 "method":"POST","path":"/dev/query","status":"200","latency":"3886",
 "principal":"kb-agent-client","tokenFingerprint":"a850ee20","authorizerError":"-"}
```

`tokenFingerprint` is the first 8 characters of the token's SHA-256. It identifies the caller
across requests **without the token ever being written down**.

## 3. Lambda structured log

`/aws/lambda/kbagent-mc-dev-query`

```json
{"level": "INFO", "message": "query_completed", "service": "kb-agent-query",
 "correlation_id": "a5e192c4-f3dd-4b0c-ad42-4d55d99b50c4",
 "xray_trace_id": "1-6aa02477-12bdc74f6b2ea1a36dc14d37",
 "session_id": "evidence", "top_score": 0.638, "confidence": 0.76, "grounding": "high",
 "chunk_ids": ["refund-and-cancellation-policy.md#chunk-1",
               "refund-and-cancellation-policy.md#chunk-0",
               "refund-and-cancellation-policy.md#chunk-2",
               "pricing-and-plans.md#chunk-6",
               "security-and-data-handling.md#chunk-3"],
 "abstained": false, "provider": "openrouter", "model": "anthropic/claude-haiku-4.5",
 "input_tokens": 1062, "output_tokens": 193, "estimated_cost_usd": 0.002027,
 "retrieval_ms": 1532.5, "generation_ms": 2188.5, "dropped_citations": [],
 "question_preview": "What is the refund policy for enterprise customers?"}
```

Everything needed to explain an answer is here: which chunks were retrieved, what they
scored, which model served the request, what it cost. `question_preview` is truncated and
sits behind `LOG_QUESTIONS`, which is off outside dev because questions are user content.

## 4. X-Ray

`xray_trace_id` in the log above is the bridge. X-Ray indexes by its own trace id, not by API
Gateway's request id, so without logging it the correlation would break at exactly this point
-- the claim that one id spans five systems would have been false for one of the five.

Reading `_X_AMZN_TRACE_ID` from the runtime environment keeps it true without adding the
X-Ray SDK, which would have broken the query Lambda's zero-dependency guarantee.

```console
$ aws xray batch-get-traces --trace-ids 1-6aa02477-12bdc74f6b2ea1a36dc14d37
Duration 3.203s, 3 segments
```

## 5. DynamoDB query log

```console
$ aws dynamodb query --table-name kbagent-mc-dev-query-log     --key-condition-expression "session_id = :s" ...

ts_request  2026-09-08T...#a5e192c4-f3dd-4b0c-ad42-4d55d99b50c4
grounding   high
confidence  0.76
model       anthropic/claude-haiku-4.5
expires_at  <30 days out>
```

Sorted by timestamp within a session, so a conversation reconstructs in order. The write is
deliberately fault-tolerant: if DynamoDB is unavailable the failure is logged and the user
still gets their answer. Logging must never take down the request path.

---

## Reproducing this

The Streamlit debug panel prints the ready-made query for any request:

```
fields @timestamp, message, confidence, top_score, latency_ms
| filter correlation_id = "<request-id>"
| sort @timestamp asc
```
