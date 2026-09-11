# ADR-11 — When this moves to containers

**Status:** **Accepted.** Lambda stays. This record exists to say, with measured numbers, the
conditions under which that answer changes.

**Outcome in one line:** exactly **one** wall forces containers — a `torch`-class dependency,
because the wheel alone is 529 MiB against Lambda's 250 MiB unzipped limit. Every other limit
investigated has a Lambda-native fix that is cheaper than a container. The wall this system
actually hits first is not a container trigger at all: **the ingest Lambda's 600 s timeout, at
~13,300 chunks.**

## Context

The brief lists this project's shape among its acceptable approaches as *"a Lambda-based API
for a small scoped implementation, **with clear notes on when you would move to containers**."*
The second half is the part that was missing. Choosing serverless is not a decision until you
can say what would unmake it.

Everything below was measured against the deployed stack `kbagent-mc-dev` or against real
`manylinux_2_28_x86_64` wheels. Where a figure is derived or estimated it says so.

## The walls, in the order this system would actually hit them

| # | Wall | Measured trigger | Forces containers? |
|---|---|---|---|
| 1 | **Ingest timeout** | **~13,300 chunks** at the deployed 600 s; ~20,000 at Lambda's 900 s hard max | ❌ Incremental reindex |
| 2 | **Documents Lambda OOM** | **~12,000–17,000 chunks** at 512 MB | ❌ Delete a needless parse |
| 3 | **Query Lambda OOM** | **~19,000 chunks** as written; ~28,000–34,000 if the reload path is fixed | ❌ Raise memory |
| 4 | **Per-query scan cost** | 0.0226 ms/chunk — ~100 ms at 5k, ~1.25 s at 50k, *on every request* | ❌ Approximate index |
| 5 | **29 s request ceiling** | API Gateway integration timeout, measured `timeoutInMillis: 29000` | ❌ Go Regional |
| 6 | **Streaming** | Python managed runtime cannot stream responses | ⚠️ Adapter, or yes |
| 7 | **Package size** | `torch` = **529 MiB compressed** vs a **250 MiB** unzipped limit | ✅ **Yes** |

### 1 · Ingest time is the real first wall — not memory

Measured on the deployed ingest Lambda from two real runs in CloudWatch (52 chunks in 2,413 ms;
1,016 chunks in 45,831 ms): **45.0 ms per chunk**, dominated by the embedding API calls.

- At the deployed **600 s** timeout: **~13,300 chunks.**
- At Lambda's **900 s** hard maximum: **~20,000 chunks.**

This arrives before every memory limit below, and it is the one place the current design is
genuinely brittle: **every change rebuilds the whole index.** The fix is incremental
reindexing — embed only what changed — which is already named in *Implemented versus proposed*
and needs no container.

### 2 · The tightest memory wall is self-inflicted

`services/documents/handler.py` parses the **entire index through `json.loads`** — every
chunk's 512 floats — purely to build a per-document chunk count, when the only field it reads
is `document_id`. At 512 MB that makes the documents Lambda the system's tightest memory
ceiling, roughly **half** the query Lambda's, for no functional reason. Streaming the count, or
having ingest write it into the artifact, removes the wall for free.

### 3 · The query Lambda, and a correction to ADR-01

Measured by loading real and synthetic artifacts through the actual `rag/index_store.py` path,
against a Lambda baseline of ~107 MB median (`Max Memory Used`, n=318 invocations):

| Path that must survive | Per chunk | Ceiling at 1024 MB |
|---|---|---|
| Steady state | ~22 KB | ~42,000 |
| Cold-start load | ~28 KB | ~28,000–34,000 |
| **Warm re-ingest reload** | **~50 KB** | **~19,000** |

The reload path is the binding one, and the reason is in the code: `index_store.load()` rebinds
`_cached_index` only *after* `from_artifact()` returns, so **the old index stays live in memory
while the new one is parsed**. Peak is both indexes at once. Fixing that is a few lines and buys
the corpus roughly 1.6×.

> **ADR-01 and `rag/retriever.py` both claim the in-memory index is viable to "roughly 50k
> chunks". That claim is false and is corrected here.** At 50,000 chunks the measured peak is
> **1,150–1,460 MiB** against a 1,024 MB allocation — it OOMs, on every accounting method three
> independent measurements tried. 50k needs a 2048 MB Lambda to cold-start and 3008 MB to
> survive a reindex. Separately, a full rebuild of 50k chunks would take **~37 minutes** against
> Lambda's 900 s maximum, so the number was unreachable twice over.

### 5 · The 29 s ceiling is real, and adjustable — by changing endpoint type, not compute

The deployed integration measures `timeoutInMillis: 29000`, which is why the query Lambda is set
to 28 s. AWS made this quota adjustable to 300 s, but the quota table lists edge-optimized REST
APIs as **"Can be increased: No"**. This API is edge-optimized. A **Regional** endpoint raises
the ceiling to 300 s — a one-line CDK change, not a container.

### 6 · Streaming: the common explanation is wrong, and the real blocker is the runtime

It is widely repeated that API Gateway cannot stream a Lambda response and that streaming
requires Function URLs. This deployment contradicts it: the integration object carries an
explicit **`"responseTransferMode": "BUFFERED"`**, and CDK exposes the field.

The actual blocker is the runtime. AWS: *"Lambda supports response streaming on Node.js managed
runtimes. For other languages, including Python, you can use a custom runtime … or use the
Lambda Web Adapter."* This service is `python3.12` managed. So streaming costs either the Web
Adapter layer, a custom runtime, or a container — and it is the **only** optional feature in the
brief where a container is a reasonable answer rather than an expensive one.

*Not verified:* whether streaming is supported on edge-optimized endpoints specifically. The AWS
documentation does not break the feature down by endpoint type.

### 7 · Package size — the one genuine wall, and a correction to ADR-02

Measured from the real wheels, summing true unzipped member sizes:

| Dependency | Unzipped | vs Lambda's 250 MiB |
|---|---|---|
| The LangGraph branch's vendored closure | 47.8 MiB | 19% — fits |
| `numpy` + `faiss-cpu` | **116.6 MiB** | **47% — fits comfortably** |
| `torch` | **529 MiB *compressed*** | **212% — impossible** |

> **ADR-02's framing needs qualifying.** It is right that an ANN index optimises 2% of the
> latency, but it implies a package-size problem that does not exist: **FAISS fits.** What
> ADR-02 actually protects is the *zero-dependency, no-Docker, deploy-from-any-OS* property, not
> a size limit. A torch-class model — a local cross-encoder re-ranker, a local embedding model —
> is the real trigger, and it is absolute: the compressed wheel alone exceeds the unzipped
> limit, so no amount of pruning reaches it. Container images allow 10 GB.

## Two walls that sound relevant and are not

- **Cold start.** Measured median **540.5 ms**, p95 596.7 ms, on 7.5% of invocations (n=24).
  If that ever mattered, **provisioned concurrency is $10.95/month against $25.44/month** for
  the cheapest always-on container that could serve this (Fargate 0.25 vCPU + the load balancer
  API Gateway REST would force you to add). Cold start never justifies containers here.
- **VPC.** A NAT gateway is **$32.85/month** — more than the entire $20 budget and more than the
  container option it is often bundled with. It forces nothing; S3, Secrets Manager and DynamoDB
  can use VPC endpoints instead.

**One limit is binding today and has a free fix:** this account's `ConcurrentExecutions` quota
is **10**, against an AWS default of 1,000. At the stage throttle of 5 rps and a measured 1.87 s
duration, Little's Law puts steady-state concurrency at **9.35 — 94% of the limit.** A quota
increase, not a rearchitecture.

## Cost: the weakest trigger of all

Modelled against live Price List API rates, the crossover where Fargate becomes cheaper than
Lambda for this workload is **roughly 600,000–1,000,000 answered queries per month**
(~20,000–34,000/day, ~0.25–0.4 sustained req/s). That is on the order of **200× this system's
actual traffic.**

Two things make the honest number lower than a naive one, and both were caught in review:

- **`latency_ms` is not billed duration.** The application's own timer reports 1,687 ms; the
  Lambda `@billedDuration` for that same invocation was **2,467 ms**. ~780 ms of handler
  overhead sits outside the app's instrumentation. Any cost model built on `latency_ms`
  understates the bill by ~40%. *(Worth fixing in the metric, not just in this ADR.)*
- **The corpus abstains more than it answers** — measured 35% answered, 65% abstained — and an
  abstention bills ~112 ms rather than ~2,800 ms.

The crossover is set by **Fargate's fixed floor**, over half of which is the load balancer, not
by the blocked-I/O asymmetry that usually motivates the move. Lambda billing wall-clock while
blocked on the model provider is real, but at this scale it loses to a $16/month idle ALB.

**Conclusion: cost alone will not move this system to containers.** If the traffic ever
justified it, the corpus would have forced an architecture change long before.

## Decision

**Stay on Lambda.** Revisit when any of these is true:

1. **A `torch`-class dependency becomes necessary** — a local re-ranker or a local embedding
   model. This is the only hard wall; act on it immediately, there is no Lambda-native fix.
2. **Response streaming becomes a requirement** and the Lambda Web Adapter is judged worse than
   a container.
3. **The corpus passes ~10,000 chunks** — not because containers are needed, but because that is
   where the in-memory design should be replaced by a real vector store (the Bedrock Knowledge
   Bases path in [ADR-07](README.md)), which changes the compute question entirely.
4. **A single request needs more than 300 s**, past what a Regional endpoint can buy.

Growth below those lines is answered by Lambda-native fixes, in this order: fix the reload
path, delete the documents Lambda's needless parse, raise the ingest timeout to 900 s, make
reindexing incremental, raise memory, raise the concurrency quota.

## Consequences

- **Two existing records are corrected by this one.** ADR-01's "~50k chunks" is false
  (measured: OOM at 1024 MB, and a 37-minute rebuild); ADR-02's package-size implication is
  false (FAISS fits at 47% of the limit). Both corrections are in the sections above, and
  `rag/retriever.py`'s docstring is updated to match.
- **Three concrete defects surfaced while measuring** and are now on the record: the reload
  path holding two indexes live, the documents Lambda parsing every embedding to count
  documents, and `latency_ms` excluding ~780 ms of billed time.
- **The honest reading:** this ADR was written to satisfy a clause in the brief, and the clause
  earned its place. Deriving the thresholds found more wrong claims in this repository than any
  test suite has.

## Method and caveats

Memory figures were measured three ways that agree — deep `sizeof` following containers,
`tracemalloc`, and process RSS — at corpus sizes from 52 to 50,000 chunks, with linearity
holding across the range. Package sizes are true unzipped member sums from real wheels, not
compressed wheel sizes. AWS limits and prices were read from the live Service Quotas and Price
List APIs.

**Load and scan timings were measured on a local workstation, not on Lambda.** At 1024 MB a
Lambda gets ~0.58 vCPU, so the real figures are **slower** than those quoted — the timing
numbers are lower bounds. Memory figures carry no such caveat. All AWS calls in this work were
read-only; nothing was deployed, invoked or modified.
