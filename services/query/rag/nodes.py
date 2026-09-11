"""The query pipeline as steps over one state dict: retrieve, generate, verify, revise.

On main the whole answered path is forty straight lines in `handler.query`. Adding the
second reading (verifier.py) turns it into a loop -- generate, check, maybe rewrite, check
again -- and this branch asks whether a graph framework earns its place running that loop.
The only way to answer that fairly is to make the framework the *only* variable, so:

*   **Every step is a plain function from state to update.** A node takes the state dict
    and returns just the keys it changes. Nothing here imports langgraph; graph.py wires
    these same functions into a `StateGraph`, and `run_loop` below drives them with a
    `while` loop. Both runners execute identical code, so any difference between them in
    cost, latency or behaviour is the runner's, and tests/test_graph.py holds their final
    states equal to make sure of it.

*   **Decisions are routers, and routers are pure.** `after_retrieve`, `after_generate`
    and `after_verify` read the state and name the next node. They cannot write, which is
    why the abstention reason is recomputed by `abstain` from the same helper the routers
    use rather than handed over: one function, three callers, no way to disagree.

*   **Appends without reducers.** `calls` grows by returning `state.get("calls", []) +
    [entry]`. LangGraph would let a reducer do this, but then the loop would need one too
    and the functions would stop being identical. Last-write-wins is the one merge rule
    both runners share, so it is the only one used.

*   **Nothing here may leave main worse off.** Should the experiment end with "keep the
    loop, drop the framework", this module and `run_loop` are what gets cherry-picked, and
    main's query Lambda stays at zero third-party dependencies (ADR-02).

`retrieve` is `handler.query`'s retrieval lines moved here unchanged, with the same
`VectorStore` call and the same cap, so both stacks retrieve byte-identically -- that is
the experiment's control, and the evaluation checks it.
"""

from __future__ import annotations

import time
from typing import Literal, TypedDict

from . import config
from .confidence import verify_citations
from .models import Hit, KnowledgeBase
from .observability import Timer, log_error
from .prompt import INSUFFICIENT_SENTINEL, build_user_message, system_prompt
from .providers import GenerationResult, ModelProvider, ProviderError
from .retriever import VectorStore
from .verifier import FAIL, VERIFIER_PROMPT, Verdict, build_verify_message, parse_verdict

# Abstention reasons, as `metadata.abstain_reason` reports them today. Unchanged from main
# because the two abstention responses are contractually identical across the stacks.
BELOW_FLOOR = "top_score_below_floor"
MODEL_INSUFFICIENT = "model_reported_insufficient_context"

# What the generator is told when the checker sent its answer back. It names the finding
# and the failed claims with the spans the checker quoted, and it leaves the decision to
# the generator: a checker can be wrong too, and a note that said "the reviewer is right,
# change your answer" would turn one misreading into two.
_REVIEWER_NOTE = (
    "A reviewer checked your previous answer against the passages and found: {issue} "
    "Failed claims: {failed}. Re-read the cited passages literally; correct the answer if "
    "the reviewer is right, keep it if the reviewer is wrong. Keep the citations."
)


class QueryState(TypedDict, total=False):
    """Everything one request accumulates on its way through the nodes.

    Every key a node may write has to be declared here: LangGraph builds one channel per
    key and rejects writes to keys it does not know, whereas the loop would accept anything.
    Declaring them all is what keeps the two runners equivalent.

    `provider` and `kb` are live objects. That is fine for both runners today and is the
    reason graph.py must never gain a checkpointer without first taking them out of the
    state: a checkpointer serialises every channel, and neither an HTTP client nor a
    52-chunk index with embeddings belongs in one.
    """

    # From the handler.
    question: str
    top_k: int
    style: str
    request_id: str
    provider: ModelProvider
    kb: KnowledgeBase
    # `time.monotonic()` at which the Lambda will be killed. Absent means no deadline.
    deadline_at: float

    # retrieve
    hits: list[Hit]
    scores: list[float]
    top_score: float
    retrieval_ms: float

    # generate (overwritten by a revision; the response reports the last generation)
    result: GenerationResult
    answer: str | None
    cited: list[str]
    dropped: list[str]
    generation_ms: float

    # verify (overwritten by the second reading, so the final verdict is always the last)
    verdict: str
    issue: str
    failed_claims: list[dict]
    claims_checked: int
    verification_ms: float
    verify_model: str

    # revise
    rounds: int
    reviewer_note: str

    # abstain
    abstain_reason: str

    # Token ledger, one entry per completed model call, so the response can report totals
    # and cost for the whole request rather than for the last generation.
    calls: list[dict]


# ---------------------------------------------------------------------------- nodes


def retrieve(state: QueryState) -> QueryState:
    """Embed the question and take the top passages, exactly as main does."""
    provider, kb = state["provider"], state["kb"]
    with Timer() as timer:
        question_vector = provider.embed(state["question"])
        hits = VectorStore(kb.chunks).search(
            question_vector,
            top_k=state.get("top_k") or config.TOP_K_DEFAULT,
            max_per_document=config.MAX_CHUNKS_PER_DOC,
        )
    scores = [hit.score for hit in hits]
    return {
        "hits": hits,
        "scores": scores,
        "top_score": scores[0] if scores else 0.0,
        "retrieval_ms": timer.ms,
    }


def generate(state: QueryState) -> QueryState:
    """Write the answer, or rewrite it when a reviewer note is present."""
    hits = state["hits"]
    with Timer() as timer:
        result = state["provider"].generate(
            system=system_prompt(state.get("style") or config.DEFAULT_STYLE),
            user=build_user_message(state["question"], hits, reviewer_note=state.get("reviewer_note")),
            max_tokens=config.MAX_OUTPUT_TOKENS,
            temperature=config.TEMPERATURE,
        )
    answer, cited, dropped = verify_citations(result.text, {hit.chunk.chunk_id for hit in hits})
    return {
        "result": result,
        "answer": answer,
        "cited": cited,
        "dropped": dropped,
        "generation_ms": state.get("generation_ms", 0.0) + timer.ms,
        "calls": state.get("calls", []) + [_ledger_entry("generate", result)],
    }


def verify(state: QueryState) -> QueryState:
    """The second reading. Never raises: a checker that cannot speak leaves the answer as
    it is and says so in the verdict, because the user asked for an answer, not a check.

    The two skips are decided here rather than in a router so that their reason is written
    into the state and surfaces in `metadata.verification` -- a router can only choose a
    path, and "skipped, deadline" and "skipped, disabled" are different facts.
    """
    if not config.VERIFY_ENABLED:
        return _verdict_update(Verdict.skipped("VERIFY_ENABLED is false"))
    if _seconds_left(state) < config.VERIFY_DEADLINE_RESERVE_MS / 1000:
        return _verdict_update(Verdict.skipped("deadline"))

    message = build_verify_message(
        state["question"], state["answer"], state["hits"], config.VERIFY_MAX_PASSAGES
    )
    result = None
    with Timer() as timer:
        try:
            result = state["provider"].generate(
                system=VERIFIER_PROMPT,
                user=message,
                max_tokens=config.VERIFY_MAX_TOKENS,
                # The checker is asked to quote, not to write; sampling would only add noise
                # to a verdict the experiment wants reproducible.
                temperature=0.0,
                model=config.VERIFY_MODEL_ID,
            )
        except (ProviderError, TimeoutError) as exc:
            # Logged, because a swallowed provider failure with no line in CloudWatch is a
            # debugging trap; carried in the verdict, because the caller deserves to know.
            log_error("verification_failed", error=type(exc).__name__, detail=str(exc)[:200])
            verdict = Verdict.unverified(f"verifier call failed: {type(exc).__name__}")
        else:
            verdict = parse_verdict(result.text)

    update = _verdict_update(verdict)
    update["verification_ms"] = state.get("verification_ms", 0.0) + timer.ms
    update["verify_model"] = result.model if result else config.VERIFY_MODEL_ID
    if result is not None:
        update["calls"] = state.get("calls", []) + [_ledger_entry("verify", result)]
    return update


def revise(state: QueryState) -> QueryState:
    """Count the round and compose the note the next generation will see."""
    failed = "; ".join(
        f'"{claim["claim"]}" ({claim["status"]}'
        + (f' by {claim["chunk_id"]}' if claim["chunk_id"] else "")
        + (f': "{claim["quote"]}"' if claim["quote"] else "")
        + ")"
        for claim in state.get("failed_claims", [])
    ) or "none named"
    issue = state.get("issue") or "the answer is not supported by the passages it cites."
    if not issue.endswith("."):
        issue += "."
    return {
        "rounds": state.get("rounds", 0) + 1,
        "reviewer_note": _REVIEWER_NOTE.format(issue=issue, failed=failed),
    }


def abstain(state: QueryState) -> QueryState:
    """Record why no answer is given. The response itself is still built by the handler's
    `_abstention`, unchanged from main, so that both abstention bodies stay identical."""
    reason = _abstain_reason(state)
    update: QueryState = {"abstain_reason": reason}
    if reason == MODEL_INSUFFICIENT:
        # The model's one-sentence explanation of what is missing, without the sentinel;
        # None when it gave none, which the handler replaces with the standard wording.
        update["answer"] = state["answer"][len(INSUFFICIENT_SENTINEL) :].strip() or None
    return update


def finalize(state: QueryState) -> QueryState:  # noqa: ARG001
    """The answered path's terminal. Deliberately a no-op: it exists so the graph and the
    loop end on the same named step and the diagram shows where answers leave. Whatever
    the last `verify` wrote is what the response reports; nothing is reconciled here, so
    a revision that made things worse cannot be dressed up as a success."""
    return {}


# -------------------------------------------------------------------------- routers
#
# Return types are Literal on purpose: graph.py reads them to draw the edges, and a router
# without one is drawn as an edge straight to END (the prototype showed it).


def after_retrieve(state: QueryState) -> Literal["generate", "abstain"]:
    return "abstain" if _abstain_reason(state) else "generate"


def after_generate(state: QueryState) -> Literal["verify", "abstain"]:
    return "abstain" if _abstain_reason(state) else "verify"


def after_verify(state: QueryState) -> Literal["revise", "finalize"]:
    if (
        state.get("verdict") == FAIL
        and state.get("rounds", 0) < config.VERIFY_MAX_ROUNDS
        # A revision costs a generation and another verification; the same reserve that
        # gates one verification is what one round needs.
        and _seconds_left(state) >= config.VERIFY_DEADLINE_RESERVE_MS / 1000
    ):
        return "revise"
    return "finalize"


# ------------------------------------------------------------------------- the loop


def run_loop(state: QueryState) -> QueryState:
    """Drive the nodes with a `while` loop: the framework-free runner the experiment
    compares the graph against. Same nodes, same routers, same final state."""
    state = dict(state)  # the caller's dict is input, not scratch space
    state.update(retrieve(state))
    if after_retrieve(state) == "abstain":
        state.update(abstain(state))
        return state
    while True:
        state.update(generate(state))
        if after_generate(state) == "abstain":
            state.update(abstain(state))
            return state
        state.update(verify(state))
        if after_verify(state) == "finalize":
            state.update(finalize(state))
            return state
        state.update(revise(state))


# --------------------------------------------------------------------------- helpers


def _abstain_reason(state: QueryState) -> str | None:
    """Why this request abstains, or None. Shared by the routers and by `abstain`."""
    if state.get("top_score", 0.0) < config.RELEVANCE_FLOOR:
        # Nothing similar enough was found; not worth paying for a generation that would
        # either hedge or invent. Fires before any generation exists.
        return BELOW_FLOOR
    if (state.get("answer") or "").startswith(INSUFFICIENT_SENTINEL):
        return MODEL_INSUFFICIENT
    return None


def _seconds_left(state: QueryState) -> float:
    deadline = state.get("deadline_at")
    return float("inf") if deadline is None else deadline - time.monotonic()


def _verdict_update(verdict: Verdict) -> QueryState:
    return {
        "verdict": verdict.verdict,
        "issue": verdict.issue,
        "failed_claims": verdict.failed_claims,
        "claims_checked": verdict.claims_checked,
    }


def _ledger_entry(stage: str, result: GenerationResult) -> dict:
    return {
        "stage": stage,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
