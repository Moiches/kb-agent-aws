"""Graph-versus-loop parity: the framework must add nothing but itself.

The experiment prices LangGraph against a while-loop over the same nodes. That comparison
is only fair if the two runners make the same decisions on the same input, so every
scenario here runs twice -- `nodes.run_loop` and `graph.run` -- and the final states and
the provider call sequences must be equal. The scenarios and the scripted provider are
test_nodes.py's; this module adds nothing to them but the second runner.

Skipped as a whole when langgraph is not installed (`tests/requirements-dev.txt`), because
the loop configuration is meant to work without it and so is the rest of the suite.
"""

import time

import pytest

pytest.importorskip("langgraph")

from rag import config, graph, nodes  # noqa: E402
from rag.providers import ErrorKind, ProviderError  # noqa: E402
from rag.verifier import FAIL, PASS, SKIPPED, UNVERIFIED  # noqa: E402
from test_nodes import (  # noqa: E402
    FAIL_REPLY,
    PASS_REPLY,
    RIGHT,
    SLA,
    WRONG,
    FakeProvider,
    initial_state,
    pin_config,
    verdict_json,
)


@pytest.fixture(autouse=True)
def pinned_config(monkeypatch):
    pin_config(monkeypatch)


# Each scenario is the provider's script plus what the final state must say, so that a
# parity failure and a plain wrong answer are told apart. `deadline` is seconds from now.
SCENARIOS = {
    "pass-first-time": dict(
        provider=dict(answers=[RIGHT], verdicts=[PASS_REPLY]),
        expect=dict(answer=RIGHT, verdict=PASS, rounds=0, cited=[SLA]),
    ),
    "fail-then-revise-then-pass": dict(
        provider=dict(answers=[WRONG, RIGHT], verdicts=[FAIL_REPLY, PASS_REPLY]),
        expect=dict(answer=RIGHT, verdict=PASS, rounds=1, cited=[SLA]),
    ),
    "fail-twice-give-up": dict(
        provider=dict(
            answers=[WRONG, WRONG],
            verdicts=[FAIL_REPLY, verdict_json("fail", status="not_found", issue="still wrong")],
        ),
        expect=dict(answer=WRONG, verdict=FAIL, rounds=1, cited=[SLA], issue="still wrong"),
    ),
    "abstain-below-floor": dict(
        provider=dict(embedding=(0.0, 1.0)),
        expect=dict(abstain_reason=nodes.BELOW_FLOOR, rounds=0),
    ),
    "abstain-model-insufficient": dict(
        provider=dict(answers=["INSUFFICIENT_CONTEXT: nothing on this."]),
        expect=dict(abstain_reason=nodes.MODEL_INSUFFICIENT, answer="nothing on this.", rounds=0),
    ),
    "deadline-skip": dict(
        provider=dict(answers=[RIGHT]),
        deadline=0.0,
        expect=dict(answer=RIGHT, verdict=SKIPPED, issue="deadline", rounds=0, cited=[SLA]),
    ),
    "verifier-unavailable": dict(
        provider=dict(answers=[RIGHT], verdicts=[ProviderError(ErrorKind.UNAVAILABLE, "down")]),
        expect=dict(answer=RIGHT, verdict=UNVERIFIED, rounds=0, cited=[SLA]),
    ),
}

# The handler's own objects and the stopwatch fields are the only parts of the final state
# that two runs cannot share; everything else is a decision and must match.
NOT_COMPARED = ("provider", "kb", "retrieval_ms", "generation_ms", "verification_ms")


def decisions(final: dict) -> dict:
    return {key: value for key, value in final.items() if key not in NOT_COMPARED}


def run_scenario(runner, scenario: dict) -> tuple[dict, FakeProvider]:
    provider = FakeProvider(**scenario["provider"])
    overrides = {}
    if "deadline" in scenario:
        overrides["deadline_at"] = time.monotonic() + scenario["deadline"]
    return runner(initial_state(provider, **overrides)), provider


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_graph_and_loop_reach_the_same_final_state(name):
    scenario = SCENARIOS[name]
    loop_final, loop_provider = run_scenario(nodes.run_loop, scenario)
    graph_final, graph_provider = run_scenario(graph.run, scenario)

    assert decisions(graph_final) == decisions(loop_final)
    assert graph_provider.calls == loop_provider.calls

    # And both are right, not merely alike.
    for key, value in scenario["expect"].items():
        assert graph_final.get(key) == value, key
    for absent in {"verdict", "abstain_reason"} - set(scenario["expect"]):
        assert absent not in graph_final


def test_the_graph_hands_back_the_objects_it_was_given():
    """No checkpointer, no serialisation: the provider and the index pass through untouched,
    which is the property that makes carrying them in the state acceptable."""
    provider = FakeProvider(answers=[RIGHT], verdicts=[PASS_REPLY])
    given = initial_state(provider)
    final = graph.run(given)
    assert final["provider"] is provider
    assert final["kb"] is given["kb"]


def test_the_recursion_limit_leaves_room_for_a_second_round(monkeypatch):
    """Twelve steps fit retrieve, three generate-verify rounds and finalize (ten), so
    turning VERIFY_MAX_ROUNDS up to 2 does not need a wiring change; a third round would."""
    monkeypatch.setattr(config, "VERIFY_MAX_ROUNDS", 2)
    provider = FakeProvider(answers=[WRONG] * 3, verdicts=[FAIL_REPLY] * 3)
    final = graph.run(initial_state(provider))
    assert final["rounds"] == 2
    assert final["verdict"] == FAIL
    assert provider.stages() == ["embed"] + ["generate", "verify"] * 3


# ---------------------------------------------------------------------------- diagram


def edges(diagram: str) -> set[tuple[str, str]]:
    """Every (source, target) pair in a Mermaid flowchart. Conditional edges are drawn
    dotted (`-.->`) and plain ones solid (`-->`); both are edges."""
    pairs = set()
    for line in diagram.splitlines():
        line = line.strip().rstrip(";")
        for arrow in ("-.->", "-->"):
            if arrow in line:
                source, target = (part.strip() for part in line.split(arrow, 1))
                pairs.add((source, target))
    return pairs


def test_the_diagram_is_drawn_from_the_real_routers():
    """A lambda in place of a Literal-typed router still runs but draws `verify --> __end__`
    (the prototype showed it). The README's picture comes from this function, so the edges
    it shows must be the ones the routers can take -- all of them, and no others."""
    diagram = graph.mermaid()
    for name in graph.NODE_NAMES:
        assert f"{name}({name})" in diagram

    drawn = edges(diagram)
    assert drawn == {
        ("__start__", "retrieve"),
        ("retrieve", "generate"), ("retrieve", "abstain"),
        ("generate", "verify"), ("generate", "abstain"),
        ("verify", "revise"), ("verify", "finalize"),
        ("revise", "generate"),
        ("abstain", "__end__"), ("finalize", "__end__"),
    }
