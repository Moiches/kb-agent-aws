# Architecture Decision Records

Short records of the decisions that shaped this solution: the context, the options weighed,
what was chosen, and what it cost. They exist so a reviewer can follow the reasoning without
reverse-engineering it from the code, and so a decision can be revisited later against the
constraints that actually produced it.

## Index

| # | Decision | Status |
|---|---|---|
| 01 | **In-memory vector store in Lambda.** No OpenSearch Serverless (prohibited by the brief and priced on continuous base capacity), no micro RDS with pgvector (would consume ~70% of a $20 budget). The index is a versioned artifact in S3, loaded into Lambda memory at cold start. Retrieval costs $0.00. | Accepted |
| 02 | **Exact cosine search in pure Python, not FAISS.** At ~52 chunks × 512 dimensions a full scan takes 3–5 ms, against 700–1600 ms for the generation call. An ANN index would optimise 2% of the latency while adding a native dependency to every deployment. Keeps the query Lambda at zero third-party dependencies: no Docker, deploys from any OS. | Accepted |
| 03 | **API Gateway REST (v1), not HTTP API (v2).** REST is ~3.5× the per-request price, which is $0.006 at this volume. It is the only variant supporting custom Gateway Responses, and therefore the only way to return the documented `{error, message, request_id}` envelope on 401/403/429 as well as on application errors — without moving authentication into the business Lambda. | Accepted |
| 04 | **Lambda TOKEN authorizer with Secrets Manager, not a native API key.** API Gateway API keys identify callers for usage plans; they are not an authentication mechanism, and AWS documents them as such. The authorizer adds a regex pre-filter that rejects malformed tokens without invoking any Lambda, and constant-time comparison against the stored secret. | Accepted |
| 05 | **One stack composed of four L3 constructs, not several stacks.** Cross-stack references produce `Fn::ImportValue` exports that block deletion while in use — friction with no benefit for a reviewer who deploys and destroys once. Constructs demonstrate the same modularity without the operational cost. | Accepted |
| 06 | **Amazon Bedrock AgentCore: considered, not used.** This is single-step RAG, not an agentic tool loop; AgentCore Runtime solves long-running multi-step sessions, which this system does not have. Also weighed: session-time billing against a $20 budget, and service-availability risk in a shared sandbox. | Accepted |
| 07 | **Amazon Bedrock Knowledge Bases: declined.** It requires a vector store, and every supported option was either prohibited (OpenSearch Serverless), continuously priced (Aurora Serverless v2), external (Pinecone, MongoDB Atlas) or new enough to be a deployment risk in a shared account. Documented as the recommended evolution path once the corpus grows. | Accepted |
| 08 | **CDK in TypeScript, application in Python.** TypeScript is CDK's first-class language with the best L2 coverage; Python is where `boto3` and the RAG ecosystem live, and matches the reference prototype. The cost is two toolchains for the reviewer, which is the common arrangement in AWS teams. | Accepted |
| **09** | **[Model provider abstraction, with OpenRouter as the default](0009-model-provider-abstraction.md)** — the sandbox lost Bedrock access mid-project (verified: `ValidationException: Operation not allowed` on both embeddings and generation). A `ModelProvider` interface keeps both implementations; `MODEL_PROVIDER` selects one. | **Accepted** |

## Reading order

ADR-09 is written out in full because it is the decision most likely to need defending: it
changed the model provider, it moved customer data outside AWS, and it was forced by an
external constraint rather than chosen freely. It also carries reproduced evidence rather
than reasoning alone.

ADR-01 through ADR-08 are summarised above and covered in the README's architecture section.
They were recorded during design in the working plan; porting them into this directory in
full is tracked as documentation work, not a change of decision.
