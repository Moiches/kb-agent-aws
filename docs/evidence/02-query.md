# Evidence: a full query, end to end

Captured against the deployed API. Verbatim, apart from the redacted token.

## Grounded answer with verified citations

```console
$ curl -X POST "$API/query" -H "Authorization: Bearer <token>"     -d '{"question":"What is the refund policy for enterprise customers?","top_k":5}'
```

```json
{
  "answer": "Enterprise customers may request a refund within **30 days from the invoice date** [refund-and-cancellation-policy.md#chunk-1]. Requests submitted after this 30-day window are not eligible for refunds, regardless of usage level.\n\nAll refunds must be **approved by the account manager** assigned to the contract before Finance processes the payment [refund-and-cancellation-policy.md#chunk-1]. Approved refunds are prorated against unused subscription time, measured in whole days from the date the request is received [refund-and-cancellation-policy.md#chunk-2]. However, certain charges are never refundable: professional services and onboarding already delivered, API overage charges, third-party marketplace fees, and custom development work accepted by the customer [refund-and-cancellation-policy.md#chunk-2].",
  "confidence": 0.76,
  "grounding": "high",
  "sources": [
    {
      "document_id": "refund-and-cancellation-policy.md",
      "document_title": "Refund and Cancellation Policy",
      "chunk_id": "refund-and-cancellation-policy.md#chunk-1",
      "section": "Refund windows by plan",
      "page": null,
      "score": 0.638,
      "excerpt": "## Refund windows by plan | Plan | Refund window | Approval required | |---|---|---| | Enterprise | 30 days from invoice date | Account manager | | Business | 14 days from invoice date | Support lead | | Starter | Non-refundable | N/A | Enterprise customers may request a refund w",
      "cited": true
    },
    {
      "document_id": "refund-and-cancellation-policy.md",
      "document_title": "Refund and Cancellation Policy",
      "chunk_id": "refund-and-cancellation-policy.md#chunk-0",
      "section": "Scope",
      "page": null,
      "score": 0.6214,
      "excerpt": "## Scope This policy governs refunds and cancellations for Northwind Analytics subscription plans. It does not cover service credits issued under the Enterprise Service Level Agreement, which are a separate remedy described in that document.",
      "cited": false
    },
    {
      "document_id": "refund-and-cancellation-policy.md",
      "document_title": "Refund and Cancellation Policy",
      "chunk_id": "refund-and-cancellation-policy.md#chunk-2",
      "section": "How refunds are calculated",
      "page": null,
      "score": 0.5132,
      "excerpt": "## How refunds are calculated Approved refunds are prorated against unused subscription time, measured in whole days from the date the request is received. Northwind does not refund partial days. The following charges are never refundable, on any plan: - Professional services and",
      "cited": true
    },
    {
      "document_id": "pricing-and-plans.md",
      "document_title": "Pricing and Plans",
      "chunk_id": "pricing-and-plans.md#chunk-6",
      "section": "Payment methods",
      "page": null,
      "score": 0.4653,
      "excerpt": "## Payment methods Starter and Business plans are billed by credit card. Enterprise customers may pay by invoice with NET 30 terms, subject to a credit check. Purchase orders are supported on Enterprise only.",
      "cited": false
    },
    {
      "document_id": "security-and-data-handling.md",
      "document_title": "Security and Data Handling",
      "chunk_id": "security-and-data-handling.md#chunk-3",
      "section": "Data retention",
      "page": null,
      "score": 0.4139,
      "excerpt": "## Data retention Active customer data is retained for the life of the account. After termination: - Production data is retained for **90 days**, then permanently deleted - Backups containing customer data expire on a rolling **35-day** schedule - Audit logs are retained for **13",
      "cited": false
    }
  ],
  "metadata": {
    "provider": "openrouter",
    "model": "anthropic/claude-haiku-4.5",
    "embedding_model": "openai/text-embedding-3-small",
    "retrieval_strategy": "in_memory_cosine_topk",
    "request_id": "a5e192c4-f3dd-4b0c-ad42-4d55d99b50c4",
    "session_id": "evidence",
    "kb_version": "2026-09-08T14:40:00+00:00",
    "chunks_searched": 52,
    "top_score": 0.638,
    "retrieval_ms": 1532.5,
    "generation_ms": 2188.5,
    "latency_ms": 3721.0,
    "input_tokens": 1062,
    "output_tokens": 193,
    "estimated_cost_usd": 0.002027,
    "confidence_components": {
      "retrieval_strength": 0.5949,
      "consensus": 0.8313,
      "citation_coverage": 1.0
    },
    "dropped_citations": []
  }
}
```

Two things worth reading closely.

`sources[].cited` shows five passages retrieved and the ones the model actually used. Citing
fewer than were retrieved is correct behaviour, not a shortfall -- the client displays both so
a reader can see what was consulted as well as what was used.

`dropped_citations` is empty, which is positive evidence that citation verification ran and
found nothing invented. When the model fabricates a plausible-looking `chunk_id`, it is
stripped from the answer and listed here.

## Abstention: a question the corpus does not cover

```console
$ curl ... -d '{"question":"Do you sign HIPAA Business Associate Agreements?"}'
```

```json
{
  "answer": "I could not find information about that in the knowledge base.",
  "confidence": 0.22,
  "grounding": "insufficient_context",
  "sources": [],
  "metadata": {
    "provider": "openrouter",
    "model": "anthropic/claude-haiku-4.5",
    "embedding_model": "openai/text-embedding-3-small",
    "retrieval_strategy": "in_memory_cosine_topk",
    "request_id": "55e4cd64-002c-406a-83e8-8e5fee6e2fc7",
    "session_id": "evidence",
    "kb_version": "2026-09-08T14:40:00+00:00",
    "chunks_searched": 52,
    "top_score": 0.3264,
    "retrieval_ms": 374.5,
    "generation_ms": 0.0,
    "latency_ms": 374.5,
    "input_tokens": 0,
    "output_tokens": 0,
    "estimated_cost_usd": 0.0,
    "confidence_components": {
      "retrieval_strength": 0.0,
      "consensus": 0.8753,
      "citation_coverage": 0.0
    },
    "dropped_citations": [],
    "abstained": true,
    "abstain_reason": "top_score_below_floor"
  }
}
```

`sources` is empty on purpose. Showing the security document beside "I could not find this"
would invite the reader to believe the answer came from it. `abstain_reason` records which of
the two abstention paths fired -- the pre-generation short circuit, or the model emitting its
sentinel.

Note this is a **200**, not an error. Refusing to answer a question the corpus does not cover
is the system working.

## Request validation at the gateway (HTTP 400)

`top_k: 99` is rejected against the request model **before any Lambda is invoked**, so a
malformed request never reaches or bills compute.

```json
{
  "error": "bad_request",
  "message": "Request body failed validation: [string \"hi\" is too short (length: 2, required minimum: 3), numeric instance is greater than the required maximum (maximum: 10, found: 99)]",
  "request_id": "bdea6384-d97b-462d-b37a-3e22ee8563e9"
}
```

---

Request id to follow into CloudWatch: `a5e192c4-f3dd-4b0c-ad42-4d55d99b50c4`
