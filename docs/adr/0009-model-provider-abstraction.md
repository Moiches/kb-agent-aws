# ADR-09: Model provider abstraction, with OpenRouter as the default

- **Status:** Accepted
- **Date:** 2026-09-07
- **Supersedes:** the model-provider half of the original design (Amazon Bedrock for both
  embeddings and generation). It does not affect any other architectural decision.

## Context

The original design ran both halves of the RAG pipeline on Amazon Bedrock: Titan Text
Embeddings V2 for vectors and Claude 3 Haiku for generation. That choice was deliberate — it
kept every byte of customer data inside AWS, authenticated with IAM instead of a
provider API key, and matched the project brief's guidance to "default to highly efficient
models like Anthropic Claude 3 Haiku."

Partway through the project, the shared sandbox lost access to Bedrock. AMCRO has an open
case with AWS about it and supplied an OpenRouter API key as a stand-in.

### Evidence

We did not take the outage on trust; we reproduced it. From the sandbox account
(`452538045104`, IAM user `candidate-22`, `us-east-1`):

```console
$ aws bedrock-runtime invoke-model \
    --model-id amazon.titan-embed-text-v2:0 \
    --body file://emb.json --cli-binary-format raw-in-base64-out out.json
An error occurred (ValidationException) when calling the InvokeModel operation:
Operation not allowed

$ aws bedrock-runtime invoke-model \
    --model-id anthropic.claude-3-haiku-20240307-v1:0 \
    --body file://gen.json --cli-binary-format raw-in-base64-out out.json
An error occurred (ValidationException) when calling the InvokeModel operation:
Operation not allowed
```

Two things follow from this, and both shaped the decision:

1. **Embeddings are blocked too, not just chat models.** Titan Text Embeddings V2 fails
   identically. A hybrid design — embeddings on Bedrock, generation elsewhere — is not
   available to us.
2. **The failure is `ValidationException`, not `AccessDeniedException`.** A missing model
   grant produces `AccessDeniedException`. `ValidationException: Operation not allowed` on
   every model is the signature of a service control policy denying `bedrock-runtime` at the
   organization level. That is not something we can fix from inside this account, and it is
   not something a `cdk deploy` can work around.

## Decision

Introduce a `ModelProvider` interface with two implementations, selected at runtime by the
`MODEL_PROVIDER` environment variable (`openrouter` | `bedrock`). **OpenRouter is the
default.**

```
services/query/rag/providers/
├── __init__.py      # get_provider() -> ModelProvider
├── base.py          # embed(text) -> list[float]
│                    # generate(system, user) -> GenerationResult
├── openrouter.py    # /api/v1/embeddings, /api/v1/chat/completions
└── bedrock.py       # bedrock-runtime: invoke_model, converse
```

OpenRouter's API is OpenAI-compatible, so a single key covers both endpoints.

### Options considered

| Option | Why not |
|---|---|
| Migrate to OpenRouter and delete the Bedrock code | Throws away working design work and makes the AWS-native story unrecoverable when access returns. Cheap now, expensive later. |
| Wait for AWS to resolve the case | Blocks the entire project on a third party's ticket queue, with no committed date. Not a plan. |
| **Interface with both implementations** | **Chosen.** Deploys today; reverts to AWS-native with one environment variable. |

### Model selection

The selection criterion here is **not** reasoning capability. This workload needs strict
instruction-following: the model must emit citations in an exact machine-parseable form
(`[refund-and-cancellation-policy.md#chunk-2]`) and must emit the literal sentinel
`INSUFFICIENT_CONTEXT:` when the retrieved passages do not answer the question. The
citation-verification and abstention logic depends on both. Optimising for benchmark
reasoning scores would be optimising for the wrong thing, and would cost more.

- **Generation:** the Claude Haiku family, via OpenRouter. It preserves the brief's original
  cost-conscious intent, keeps prompt behaviour close to what the system was designed
  against, and is strong at exact-format output for its price tier.
- **Embeddings:** `text-embedding-3-small` with `dimensions: 512`, which preserves the
  existing index format and the pure-Python dot-product retriever (see ADR-02).

Exact model slugs are read from `openrouter.ai/models` at implementation time rather than
hard-coded from memory, because they change.

### Verified, not assumed (2026-09-07)

Both slugs and both behavioural assumptions were measured with
`scripts/check_openrouter.py` before any code was written against them.

| Check | Result |
|---|---|
| `openai/text-embedding-3-small` resolves | Yes, 625 ms |
| Native dimensionality | 1536 |
| `dimensions: 512` honoured | Yes, so the index stays small and the pure-Python retriever stays fast |
| Vector L2 norm at 512 dimensions | 1.000238 |
| `anthropic/claude-haiku-4.5` resolves | Yes, 953 ms |
| Returns an exact requested string | Yes |

Three of those readings deserve comment.

**The norm is 1.000238, and that counts as normalized.** A drift of 2.4e-4 is float32
rounding accumulated over 512 squared terms, not a missing normalization step; a genuinely
unnormalized vector would be off by orders of magnitude, not parts in ten thousand. The
practical effect is to shift a 0.91 similarity score by 0.0002, which cannot change a
ranking. The probe's first version reported this as a failure because its tolerance was
1e-4. That was too strict, and it has been corrected to distinguish float32 noise from a
real defect.

**Ingestion normalizes explicitly anyway.** Not because the measurement demanded it, but
because OpenRouter is a *router*: the same slug can be served by a different upstream
tomorrow, and nothing in the contract promises unit vectors. Two lines at ingest time turn
an observed property into a guaranteed one, and the failure it prevents is silent, since
scores would be subtly wrong with nothing crashing.

**Latency is materially higher than a same-region AWS call.** 625 ms to embed and 953 ms
for a six-token completion, against 40-90 ms measured for Bedrock Titan in-region. That is
the price of leaving AWS, and it raises the expected end-to-end p50 above the original
estimate. Not a problem at this scale, but it belongs in the record rather than in a
footnote.

## Consequences

### Positive

- The system deploys and demos today, independent of how AWS resolves the case.
- When Bedrock access returns, restoring the AWS-native path is one environment variable.
  That switch can be demonstrated live rather than described.
- The abstraction was not speculative. `generator.py` was already designed as a seam for
  swapping models; an unplanned incident exercised it. That is the useful kind of evidence
  that a boundary was drawn in the right place.

### Negative

These are the real costs, stated plainly.

- **Customer data now leaves AWS.** Retrieved document chunks and the user's question are
  sent to OpenRouter inside the generation prompt. The original design's strongest privacy
  property is gone while OpenRouter is the active provider. Mitigations: the key lives in
  Secrets Manager and never in the repository, the sample knowledge base contains no personal
  data, question logging can be disabled with `LOG_QUESTIONS=false`, and
  `MODEL_PROVIDER=bedrock` returns every model call to AWS.
- **A second secret and a second failure domain.** The query Lambda now needs
  `secretsmanager:GetSecretValue` (previously only the authorizer did), and the system now
  depends on internet egress and on a third party's availability and rate limits.
- **The Bedrock implementation cannot be tested in this environment.** It is written against
  the documented API and covered only by unit tests with a mocked client. It is *implemented*,
  not *verified*, and is labelled as such wherever it is described.
- **Provider cost sits outside the AWS budget.** The $20 sandbox budget no longer covers
  inference. The AWS side gains headroom; OpenRouter spend is tracked separately.

### Neutral

The existing cost controls transfer unchanged: reserved concurrency, stage throttling,
`maxTokens` capping, and the pre-generation abstention short-circuit all constrain provider
spend exactly as they constrained Bedrock spend.

## What this did not change

Worth recording, because it is the strongest available evidence about the architecture:

**A forced change of LLM provider touched exactly one layer.** ADR-01 (in-memory vector
store), ADR-02 (pure-Python exact cosine search), ADR-03 (REST API Gateway), ADR-04 (Lambda
token authorizer), ADR-05 (single stack, four constructs) and ADR-08 (TypeScript CDK) are all
unaffected. Retrieval, authentication, infrastructure and the API contract did not move.

Two earlier decisions also gained support from this incident:

- **ADR-06** declined Bedrock AgentCore partly on service-availability risk in a shared
  sandbox. AgentCore depends on Bedrock, so it would be unusable today.
- **ADR-07** declined Bedrock Knowledge Bases on the same reasoning. Same outcome.

Both were judged against a risk that then materialised.

One capability is genuinely lost: **Bedrock Guardrails** is no longer available as the
content-filtering and PII-blocking layer proposed for production hardening. A replacement
would need to be provider-side or implemented in the orchestration layer.
