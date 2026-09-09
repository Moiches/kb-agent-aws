# Evidence of execution

Captured against the deployed stack in account `908764745394`, `us-east-1`, on 2026-09-08.
Everything here is verbatim output, not a mock-up.

| File | What it shows |
|---|---|
| [`01-authentication.md`](01-authentication.md) | All four auth paths: no token, malformed token, wrong token, correct token |
| [`02-query.md`](02-query.md) | A full grounded answer, an abstention, and gateway-level request validation |
| [`03-observability.md`](03-observability.md) | One `request_id` followed through five systems |
| `streamlit-answer.jpg` | The local client: answer, grounding badge, confidence, sources |
| `streamlit-sources.jpg` | Source panel with per-passage scores and cited/not-cited marks |

## The demo flow the brief asks for

1. **Deploy with CDK** — `npm run deploy`. Stack outputs give the API URL, the seed command
   and the token command.
2. **Seed the knowledge base** — `npm run seed`. Returns
   `{"seeded": true, "documents": 8, "chunks": 52}`.
3. **Configure the client** — `npm run configure` reads the URL from stack outputs and the
   token from Secrets Manager and writes `.streamlit/secrets.toml`. The token never passes
   through a clipboard.
4. **Start Streamlit** — `npm run client`.
5. **Ask a question** — see `streamlit-answer.jpg`.
6. **The request reaches AWS** — see `01-authentication.md` and `02-query.md`.
7. **Answer, sources and metadata are displayed** — see the screenshots.
8. **Logs and request ids for debugging** — see `03-observability.md`. The client's Debug
   panel prints the ready-made Logs Insights query for the request just made.

## What the screenshot shows

`streamlit-answer.jpg` is worth reading closely:

- **The answer carries an inline citation** (`[refund-and-cancellation-policy.md#chunk-1]`)
  that the API verified against what was actually retrieved before returning it.
- **"Sources — 1 of 5 retrieved passages were cited"** distinguishes retrieved from used.
  Citing one of five is correct here: the other four did not answer the question. The client
  shows all five anyway so a reader can see what was consulted.
- **The badge reads "Medium — Grounded, but worth checking the sources below"**, not a bare
  `0.67`. A number tells a user nothing; an instruction does.
- **Per-source scores and section paths** — "Refund and Cancellation Policy › Refund windows
  by plan" rather than "chunk 1". That comes from the header-aware chunking.

The excerpts render as plain text rather than formatted Markdown. That is deliberate: they
are raw source text, and letting Streamlit render `## Refund windows by plan` as a heading
misrepresented what the retriever had matched on.

## Reproducing

Everything above is reproducible with the commands in the README. The evaluation harness
regenerates its own results:

```bash
python scripts/eval_run.py --stack <stack-name>
```
