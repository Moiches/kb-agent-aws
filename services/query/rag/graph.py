"""The LangGraph runner: nodes.py wired into a `StateGraph`.

This is the one module in the query Lambda that imports langgraph, and the handler imports
it only when `config.ORCHESTRATOR` says "langgraph". Everything the graph does is done by
functions that live in nodes.py and know nothing about it; what this file adds is the
wiring and the framework's own machinery, which is exactly the thing the experiment prices.
Importing it costs about 1.3 s on a warm developer machine (langgraph pulls in
langchain_core, langsmith, pydantic and zstandard eagerly), so the import is lazy by design
and the loop configuration never pays it.

The compiled graph is built once at import and shared across warm invocations. It carries
no state of its own -- every request's state is passed to `run` -- which is why one
instance is safe to share.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from . import nodes
from .nodes import QueryState

# The longest path is retrieve, generate, verify, revise, generate, verify, finalize: seven
# steps with VERIFY_MAX_ROUNDS at 1. Twelve leaves room for a second round if that knob is
# ever turned to 2, and still turns a wiring mistake into a GraphRecursionError rather than
# an unbounded loop against a paid API.
RECURSION_LIMIT = 12

NODE_NAMES = ("retrieve", "generate", "verify", "revise", "abstain", "finalize")


def _build():
    builder = StateGraph(QueryState)
    for name in NODE_NAMES:
        builder.add_node(name, getattr(nodes, name))

    builder.add_edge(START, "retrieve")
    # The routers are the Literal-typed functions from nodes.py, passed as they are. A
    # lambda in their place still runs correctly but has no return annotation for LangGraph
    # to read, and the diagram then shows "verify --> __end__" instead of the real edges.
    builder.add_conditional_edges("retrieve", nodes.after_retrieve)
    builder.add_conditional_edges("generate", nodes.after_generate)
    builder.add_conditional_edges("verify", nodes.after_verify)
    builder.add_edge("revise", "generate")
    builder.add_edge("abstain", END)
    builder.add_edge("finalize", END)

    # No checkpointer, and none may be added while the state carries the provider and the
    # knowledge base (see QueryState): a checkpointer serialises every channel on every
    # step, and those two objects are neither serialisable nor small.
    return builder.compile()


GRAPH = _build()


def run(state: QueryState) -> QueryState:
    """Run one request through the graph and return its final state."""
    return GRAPH.invoke(state, config={"recursion_limit": RECURSION_LIMIT})


def mermaid() -> str:
    """The graph as Mermaid, for the README. Drawn from the compiled graph, not by hand, so
    the picture cannot drift from the wiring."""
    return GRAPH.get_graph().draw_mermaid()
