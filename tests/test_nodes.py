"""The nodes, the routers and the while-loop runner, driven by a scripted provider.

No network and no framework: everything here runs on `nodes.run_loop`. tests/test_graph.py
reuses this module's provider and scenarios to hold the LangGraph runner to the same
results, which is why the fixtures are plain importable functions rather than conftest
fixtures.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from rag import config, nodes
from rag.confidence import grounding_label
from rag.models import Chunk, KnowledgeBase
from rag.prompt import build_user_message
from rag.providers import ErrorKind, GenerationResult, ProviderError
from rag.verifier import FAIL, PASS, SKIPPED, UNVERIFIED, VERIFIER_PROMPT

QUESTION = "What service credit applies if uptime drops to 99.0%?"
SLA = "enterprise-sla.md#chunk-2"
REFUND = "refund-and-cancellation-policy.md#chunk-1"

# The recorded Q2 failure in miniature: the passage says 10%, the first answer says none.
WRONG = f"No service credit applies at 99.0% [{SLA}]."
RIGHT = f"A 10% service credit applies at 99.0% [{SLA}]."


def knowledge_base() -> KnowledgeBase:
    """Two chunks on 2D unit vectors, as in test_retriever.py, so the scores are readable:
    against the (1, 0) embedding the SLA chunk scores 1.0 and the refund chunk 0.95;
    against (0, 1) they score 0.0 and 0.31, both under the 0.40 floor."""
    return KnowledgeBase(
        chunks=[
            Chunk(SLA, "enterprise-sla.md",
                  "Below 99.9% but at or above 99.0%: 10% of monthly fee.", [1.0, 0.0],
                  section="Service credits"),
            Chunk(REFUND, "refund-and-cancellation-policy.md",
                  "Refund requests are accepted within 30 days.", [0.95, 0.31]),
        ],
        kb_version="test",
        dimensions=2,
        document_count=2,
    )


class FakeProvider:
    """Scripted answers and verdicts; records every call it receives.

    Generation calls consume `answers` in order and verification calls consume `verdicts`
    (told apart by the system prompt, which is how the real provider would see them too).
    A scripted item that is an exception is raised instead of returned. `embedding` is what
    `embed` returns, which is how a test decides whether retrieval clears the floor.
    """

    name = "fake"
    generation_model = "fake/generator"
    embedding_model = "fake/embedder"

    GENERATE_TOKENS = (300, 60)
    VERIFY_TOKENS = (40, 25)

    def __init__(self, answers=(), verdicts=(), *, embedding=(1.0, 0.0)):
        self.answers = list(answers)
        self.verdicts = list(verdicts)
        self.embedding = list(embedding)
        self.calls: list[dict] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append({"stage": "embed", "text": text})
        return self.embedding

    def generate(self, system, user, max_tokens, temperature, model=None) -> GenerationResult:
        stage = "verify" if system == VERIFIER_PROMPT else "generate"
        self.calls.append({
            "stage": stage, "system": system, "user": user,
            "max_tokens": max_tokens, "temperature": temperature, "model": model,
        })
        script = (self.verdicts if stage == "verify" else self.answers).pop(0)
        if isinstance(script, BaseException):
            raise script
        input_tokens, output_tokens = self.VERIFY_TOKENS if stage == "verify" else self.GENERATE_TOKENS
        return GenerationResult(
            text=script, model=model or self.generation_model,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )

    def stages(self) -> list[str]:
        return [call["stage"] for call in self.calls]

    def calls_to(self, stage: str) -> list[dict]:
        return [call for call in self.calls if call["stage"] == stage]


def verdict_json(verdict, *, status="supported", issue="", claim="No credit applies at 99.0%",
                 quote="at or above 99.0%: 10% of monthly fee") -> str:
    return json.dumps({
        "claims": [{"claim": claim, "chunk_id": SLA, "status": status, "quote": quote}],
        "verdict": verdict,
        "issue": issue,
    })


PASS_REPLY = verdict_json("pass")
FAIL_REPLY = verdict_json("fail", status="contradicted", issue="99.0% is inside the first row")


def initial_state(provider, **overrides) -> dict:
    """What the handler hands the orchestrator, per the plan for step 6."""
    state = {
        "question": QUESTION, "top_k": 5, "style": "standard", "request_id": "req-1",
        "provider": provider, "kb": knowledge_base(), "rounds": 0, "calls": [],
    }
    state.update(overrides)
    return state


def pin_config(monkeypatch) -> None:
    """The knobs these tests depend on, pinned so a developer's environment (or a future
    default) cannot silently change what a scenario means."""
    monkeypatch.setattr(config, "VERIFY_ENABLED", True)
    monkeypatch.setattr(config, "VERIFY_MODEL_ID", "fake/checker")
    monkeypatch.setattr(config, "VERIFY_MAX_ROUNDS", 1)
    monkeypatch.setattr(config, "VERIFY_MAX_TOKENS", 400)
    monkeypatch.setattr(config, "VERIFY_MAX_PASSAGES", 4)
    monkeypatch.setattr(config, "VERIFY_DEADLINE_RESERVE_MS", 9000)
    monkeypatch.setattr(config, "RELEVANCE_FLOOR", 0.40)
    monkeypatch.setattr(config, "MAX_CHUNKS_PER_DOC", 3)


@pytest.fixture(autouse=True)
def pinned_config(monkeypatch):
    pin_config(monkeypatch)


# ------------------------------------------------------------------------ abstentions


def test_below_the_floor_costs_one_embedding_and_no_generation():
    provider = FakeProvider(embedding=(0.0, 1.0))
    final = nodes.run_loop(initial_state(provider))

    assert provider.stages() == ["embed"]
    assert final["abstain_reason"] == nodes.BELOW_FLOOR
    assert final["top_score"] < config.RELEVANCE_FLOOR
    # Retrieval happened and is reported; nothing after it did.
    assert len(final["hits"]) == 2
    assert "answer" not in final and "verdict" not in final


def test_the_models_own_abstention_is_never_sent_to_the_checker():
    provider = FakeProvider(answers=["INSUFFICIENT_CONTEXT: the passages do not cover weekend support."])
    final = nodes.run_loop(initial_state(provider))

    assert provider.stages() == ["embed", "generate"]
    assert final["abstain_reason"] == nodes.MODEL_INSUFFICIENT
    # The explanation, without the sentinel, is what the handler returns as the answer.
    assert final["answer"] == "the passages do not cover weekend support."
    assert "verdict" not in final


def test_a_bare_sentinel_leaves_the_wording_to_the_handler():
    final = nodes.run_loop(initial_state(FakeProvider(answers=["INSUFFICIENT_CONTEXT:"])))
    assert final["abstain_reason"] == nodes.MODEL_INSUFFICIENT
    assert final["answer"] is None


# ------------------------------------------------------------------------------ skips


def test_verification_off_is_reported_as_skipped_not_silently_absent(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_ENABLED", False)
    provider = FakeProvider(answers=[WRONG])
    final = nodes.run_loop(initial_state(provider))

    assert provider.stages() == ["embed", "generate"]
    assert final["verdict"] == SKIPPED
    assert "VERIFY_ENABLED" in final["issue"]
    assert final["answer"] == WRONG
    assert final["rounds"] == 0


def test_an_exhausted_deadline_skips_the_check_and_keeps_the_answer():
    provider = FakeProvider(answers=[WRONG])
    final = nodes.run_loop(initial_state(provider, deadline_at=time.monotonic()))

    assert provider.stages() == ["embed", "generate"]
    assert final["verdict"] == SKIPPED
    assert final["issue"] == "deadline"
    assert final["answer"] == WRONG


def test_a_deadline_inside_the_reserve_counts_as_exhausted():
    """The reserve is what one verification needs; starting one with less is how a Lambda
    times out with an answer already in hand."""
    provider = FakeProvider(answers=[WRONG])
    final = nodes.run_loop(initial_state(provider, deadline_at=time.monotonic() + 8.0))
    assert final["verdict"] == SKIPPED
    assert "verify" not in provider.stages()


def test_an_ample_deadline_verifies():
    provider = FakeProvider(answers=[RIGHT], verdicts=[PASS_REPLY])
    final = nodes.run_loop(initial_state(provider, deadline_at=time.monotonic() + 60.0))
    assert final["verdict"] == PASS
    assert provider.stages() == ["embed", "generate", "verify"]


# ------------------------------------------------------------------------- unverified


@pytest.mark.parametrize("failure", [
    ProviderError(ErrorKind.THROTTLED, "HTTP 429"),
    ProviderError(ErrorKind.UNAVAILABLE, "provider unavailable"),
    TimeoutError("socket timed out"),
])
def test_a_checker_that_cannot_speak_leaves_the_answer_unchanged(failure):
    """The user asked for an answer, not a check. A failing verifier must never turn a
    successful generation into a 5xx."""
    provider = FakeProvider(answers=[WRONG], verdicts=[failure])
    final = nodes.run_loop(initial_state(provider))

    assert provider.stages() == ["embed", "generate", "verify"]
    assert final["verdict"] == UNVERIFIED
    assert type(failure).__name__ in final["issue"]
    assert final["answer"] == WRONG
    assert final["rounds"] == 0
    # The failed call has no usage to report, so the ledger carries the generation alone.
    assert [entry["stage"] for entry in final["calls"]] == ["generate"]
    assert final["verify_model"] == "fake/checker"


def test_garbage_from_the_checker_is_unverified_and_still_costed():
    provider = FakeProvider(answers=[WRONG], verdicts=["I would rather not say."])
    final = nodes.run_loop(initial_state(provider))

    assert final["verdict"] == UNVERIFIED
    assert final["claims_checked"] == 0
    assert final["answer"] == WRONG
    # The call happened and was paid for, so it is in the ledger even though it said nothing.
    assert [entry["stage"] for entry in final["calls"]] == ["generate", "verify"]


# ------------------------------------------------------------------------ happy paths


def test_pass_first_time_is_embed_generate_verify():
    provider = FakeProvider(answers=[RIGHT], verdicts=[PASS_REPLY])
    final = nodes.run_loop(initial_state(provider))

    assert provider.stages() == ["embed", "generate", "verify"]
    assert final["verdict"] == PASS
    assert final["rounds"] == 0
    assert final["answer"] == RIGHT
    assert final["cited"] == [SLA]
    assert final["failed_claims"] == []
    assert final["claims_checked"] == 1
    assert "reviewer_note" not in final
    assert "abstain_reason" not in final


def test_the_checker_runs_on_its_own_model_budget_and_temperature():
    provider = FakeProvider(answers=[RIGHT], verdicts=[PASS_REPLY])
    final = nodes.run_loop(initial_state(provider))

    (call,) = provider.calls_to("verify")
    assert call["model"] == "fake/checker"
    assert call["max_tokens"] == 400
    assert call["temperature"] == 0.0
    assert call["system"] == VERIFIER_PROMPT
    assert f"<answer>\n{RIGHT}\n</answer>" in call["user"]
    assert f'chunk_id="{SLA}"' in call["user"]
    assert final["verify_model"] == "fake/checker"


def test_the_checker_sees_at_most_max_passages(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_MAX_PASSAGES", 1)
    provider = FakeProvider(answers=["Nothing is cited here."], verdicts=[PASS_REPLY])
    nodes.run_loop(initial_state(provider))

    (call,) = provider.calls_to("verify")
    assert call["user"].count("<passage ") == 1


def test_fail_then_revise_then_pass():
    provider = FakeProvider(answers=[WRONG, RIGHT], verdicts=[FAIL_REPLY, PASS_REPLY])
    final = nodes.run_loop(initial_state(provider))

    assert provider.stages() == ["embed", "generate", "verify", "generate", "verify"]
    assert final["rounds"] == 1
    assert final["verdict"] == PASS
    assert final["answer"] == RIGHT
    assert final["cited"] == [SLA]
    assert final["failed_claims"] == []


def test_the_reviewer_note_reaches_the_second_generation_and_only_the_second():
    provider = FakeProvider(answers=[WRONG, RIGHT], verdicts=[FAIL_REPLY, PASS_REPLY])
    final = nodes.run_loop(initial_state(provider))

    first, second = provider.calls_to("generate")
    assert "<reviewer_note>" not in first["user"]
    assert "<reviewer_note>" in second["user"]
    # The note names the finding, the failed claim and the span the checker quoted, and
    # leaves the decision to the generator.
    note = final["reviewer_note"]
    assert "99.0% is inside the first row" in note
    assert "No credit applies at 99.0%" in note
    assert "at or above 99.0%: 10% of monthly fee" in note
    assert "keep it if the reviewer is wrong" in note
    assert "Keep the citations" in note


def test_the_first_generation_sends_exactly_what_main_sends():
    """The control of the experiment: retrieval and the first answer must be comparable
    across the two stacks, so nothing about verification may leak into the first prompt."""
    provider = FakeProvider(answers=[RIGHT], verdicts=[PASS_REPLY])
    final = nodes.run_loop(initial_state(provider))

    (call,) = provider.calls_to("generate")
    assert call["user"] == build_user_message(QUESTION, final["hits"])
    assert call["max_tokens"] == config.MAX_OUTPUT_TOKENS
    assert call["temperature"] == config.TEMPERATURE
    assert call["model"] is None


def test_a_revision_that_is_still_wrong_is_reported_as_fail_not_as_revised():
    """The response reports the verdict of the LAST reading. A second answer that the
    checker also rejects cannot be dressed up as a success because a revision happened,
    and the failed claims it carries are the second reading's, not the first's."""
    second_fail = verdict_json("fail", status="not_found", issue="the revised claim has no passage",
                               claim="Credits are paid in cash", quote="")
    provider = FakeProvider(answers=[WRONG, "Credits are paid in cash [enterprise-sla.md#chunk-2]."],
                            verdicts=[FAIL_REPLY, second_fail])
    final = nodes.run_loop(initial_state(provider))

    assert provider.stages() == ["embed", "generate", "verify", "generate", "verify"]
    assert final["rounds"] == 1
    assert final["verdict"] == FAIL
    assert final["issue"] == "the revised claim has no passage"
    assert final["failed_claims"] == [
        {"claim": "Credits are paid in cash", "chunk_id": SLA, "status": "not_found", "quote": ""},
    ]
    assert final["answer"].startswith("Credits are paid in cash")


def test_max_rounds_zero_verifies_but_never_revises(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_MAX_ROUNDS", 0)
    provider = FakeProvider(answers=[WRONG], verdicts=[FAIL_REPLY])
    final = nodes.run_loop(initial_state(provider))

    assert provider.stages() == ["embed", "generate", "verify"]
    assert final["verdict"] == FAIL
    assert final["rounds"] == 0
    assert final["answer"] == WRONG


def test_every_returned_citation_is_in_the_retrieved_set_after_a_revision():
    """A revision goes through `verify_citations` like a first answer: an id the reviewer
    note may have tempted the model to invent is dropped, not returned."""
    revised = f"A 10% credit applies [{SLA}], see also [ghost-policy.md#chunk-9]."
    provider = FakeProvider(answers=[WRONG, revised], verdicts=[FAIL_REPLY, PASS_REPLY])
    final = nodes.run_loop(initial_state(provider))

    retrieved = {hit.chunk.chunk_id for hit in final["hits"]}
    assert set(final["cited"]) <= retrieved
    assert final["cited"] == [SLA]
    assert final["dropped"] == ["ghost-policy.md#chunk-9"]
    assert "ghost-policy" not in final["answer"]


# ------------------------------------------------------------------------- the ledger


def test_tokens_are_summed_across_every_model_call():
    provider = FakeProvider(answers=[WRONG, RIGHT], verdicts=[FAIL_REPLY, PASS_REPLY])
    final = nodes.run_loop(initial_state(provider))

    ledger = final["calls"]
    assert [entry["stage"] for entry in ledger] == ["generate", "verify", "generate", "verify"]
    assert [entry["model"] for entry in ledger] == ["fake/generator", "fake/checker"] * 2
    assert sum(entry["input_tokens"] for entry in ledger) == 2 * (300 + 40)
    assert sum(entry["output_tokens"] for entry in ledger) == 2 * (60 + 25)


def test_stage_timings_accumulate_across_rounds(monkeypatch):
    """`generation_ms` is the sum over every generation, `verification_ms` over every
    check, so `latency_ms` in the response covers the whole request. A fixed stopwatch
    makes the sums exact."""

    class SevenMillis:
        def __enter__(self):
            self.ms = 7.0
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(nodes, "Timer", SevenMillis)
    provider = FakeProvider(answers=[WRONG, RIGHT], verdicts=[FAIL_REPLY, PASS_REPLY])
    final = nodes.run_loop(initial_state(provider))

    assert final["retrieval_ms"] == 7.0
    assert final["generation_ms"] == 14.0
    assert final["verification_ms"] == 14.0


# ---------------------------------------------------------------------------- routers


@pytest.mark.parametrize("top_score,expected", [(0.39, "abstain"), (0.40, "generate"), (0.9, "generate")])
def test_after_retrieve_routes_on_the_floor(top_score, expected):
    assert nodes.after_retrieve({"top_score": top_score}) == expected


def test_after_generate_routes_on_the_sentinel():
    assert nodes.after_generate({"top_score": 0.9, "answer": "INSUFFICIENT_CONTEXT: x"}) == "abstain"
    assert nodes.after_generate({"top_score": 0.9, "answer": "An answer."}) == "verify"


@pytest.mark.parametrize("verdict,rounds,expected", [
    (FAIL, 0, "revise"),
    (FAIL, 1, "finalize"),
    (PASS, 0, "finalize"),
    (UNVERIFIED, 0, "finalize"),
    (SKIPPED, 0, "finalize"),
])
def test_after_verify_revises_only_a_fail_with_rounds_to_spare(verdict, rounds, expected):
    assert nodes.after_verify({"verdict": verdict, "rounds": rounds}) == expected


def test_after_verify_refuses_a_revision_inside_the_deadline_reserve():
    state = {"verdict": FAIL, "rounds": 0, "deadline_at": time.monotonic() + 1.0}
    assert nodes.after_verify(state) == "finalize"
    state["deadline_at"] = time.monotonic() + 60.0
    assert nodes.after_verify(state) == "revise"


def test_the_routers_and_abstain_agree_on_the_reason():
    """The routers cannot write state, so `abstain` recomputes the reason; one helper
    serves all three, and this pins that they cannot drift apart."""
    below = {"top_score": 0.1}
    assert nodes.after_retrieve(below) == "abstain"
    assert nodes.abstain(below) == {"abstain_reason": nodes.BELOW_FLOOR}

    insufficient = {"top_score": 0.9, "answer": "INSUFFICIENT_CONTEXT: no such policy"}
    assert nodes.after_generate(insufficient) == "abstain"
    assert nodes.abstain(insufficient) == {
        "abstain_reason": nodes.MODEL_INSUFFICIENT, "answer": "no such policy",
    }


# --------------------------------------------------------------------------- the loop


def test_run_loop_does_not_mutate_the_callers_state():
    provider = FakeProvider(answers=[RIGHT], verdicts=[PASS_REPLY])
    given = initial_state(provider)
    snapshot = dict(given)
    final = nodes.run_loop(given)
    assert given == snapshot
    assert final is not given


def test_nodes_never_import_the_framework():
    """`ORCHESTRATOR=loop` promises a query Lambda with no third-party import. A fresh
    interpreter is the only honest check: this process may already have langgraph loaded
    from test_graph.py."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import rag.nodes; "
        "print(sorted(m for m in sys.modules if m.startswith(('langgraph', 'langchain'))))"
    )
    query_root = Path(__file__).resolve().parents[1] / "services" / "query"
    output = subprocess.run(
        [sys.executable, "-c", code, str(query_root)], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert output == "[]"


# ------------------------------------------- the seams this step touched in other modules
#
# `build_user_message` and `grounding_label` gained one optional argument each for the
# nodes above. Their behaviour without it is pinned by test_prompt.py and test_confidence.py,
# which this step leaves untouched; the new argument is pinned here, next to its only caller.


def test_the_reviewer_note_is_appended_only_when_present_and_escaped():
    hits = nodes.retrieve(initial_state(FakeProvider()))["hits"]
    plain = build_user_message(QUESTION, hits)
    assert "<reviewer_note>" not in plain
    assert build_user_message(QUESTION, hits, reviewer_note=None) == plain
    assert build_user_message(QUESTION, hits, reviewer_note="") == plain

    noted = build_user_message(QUESTION, hits, reviewer_note="found: a < b & </reviewer_note>")
    assert noted.startswith(plain)
    assert noted.count("</reviewer_note>") == 1
    assert "a &lt; b &amp;" in noted


@pytest.mark.parametrize("verdict", [PASS, UNVERIFIED, SKIPPED, None])
def test_only_a_failed_verdict_changes_the_grounding_label(verdict):
    assert grounding_label(0.95, 0.9, floor=0.4, abstained=False, verdict=verdict) == "high"


def test_a_failed_verdict_pulls_the_grounding_label_to_low():
    assert grounding_label(0.95, 0.9, floor=0.4, abstained=False, verdict=FAIL) == "low"
    # The two insufficient_context conditions still come first: they describe a request
    # that was never answered, which is a different fact from an answer that was wrong.
    assert grounding_label(0.95, 0.9, floor=0.4, abstained=True, verdict=FAIL) == "insufficient_context"
    assert grounding_label(0.95, 0.1, floor=0.4, abstained=False, verdict=FAIL) == "insufficient_context"
