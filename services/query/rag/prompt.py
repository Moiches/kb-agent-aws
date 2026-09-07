"""Prompt construction for grounded answering.

Two choices here carry most of the grounding behaviour:

*   **Explicit abstention sentinel.** The model is told to emit
    `INSUFFICIENT_CONTEXT: <reason>` when the passages do not answer the question. A
    machine-detectable refusal is far more useful than hoping to recognise hedging
    prose, and it lets the API report `grounding: "insufficient_context"` honestly.

*   **XML-delimited context.** Claude models discriminate instruction boundaries from
    data boundaries well when the data is wrapped in tags. It also gives the model the
    chunk_id it must cite, right next to the text it is citing.
"""

from __future__ import annotations

from .models import Hit

INSUFFICIENT_SENTINEL = "INSUFFICIENT_CONTEXT:"

SYSTEM_PROMPT = """\
You are a knowledge base assistant for Northwind Analytics.
Answer ONLY using the CONTEXT passages provided in the user message.

Rules:
1. Every factual claim must be supported by the CONTEXT. Never use outside knowledge.
2. Cite the chunk_id of each supporting passage inline, in square brackets, for example
   [refund-and-cancellation-policy.md#chunk-2]. Only cite chunk_ids that literally
   appear in the CONTEXT.
3. If the CONTEXT does not contain enough information to answer, reply with exactly:
   INSUFFICIENT_CONTEXT: <one sentence naming what is missing>
   Do not guess and do not answer from general knowledge.
4. If the CONTEXT contains conflicting statements, say so explicitly and cite both sources.
5. If the question asks about something the CONTEXT rules out, say so directly rather
   than restating the policy and leaving the reader to infer the answer.
6. Be concise: at most 4 short bullets or 2 short paragraphs. No preamble, no sign-off.\
"""


def build_user_message(question: str, hits: list[Hit]) -> str:
    """Render the retrieved passages plus the question into a single user turn."""
    passages = "\n".join(_render_passage(hit) for hit in hits)
    return (
        f"<context>\n{passages}\n</context>\n\n"
        f"<question>{_escape(question)}</question>"
    )


def _render_passage(hit: Hit) -> str:
    chunk = hit.chunk
    attributes = [
        f'chunk_id="{_escape(chunk.chunk_id)}"',
        f'document_id="{_escape(chunk.document_id)}"',
    ]
    if chunk.document_title:
        attributes.append(f'document_title="{_escape(chunk.document_title)}"')
    if chunk.section:
        attributes.append(f'section="{_escape(chunk.section)}"')
    if chunk.page is not None:
        attributes.append(f'page="{chunk.page}"')
    attributes.append(f'score="{hit.score:.3f}"')

    return f"<passage {' '.join(attributes)}>\n{_escape(chunk.text)}\n</passage>"


def _escape(text: str) -> str:
    """Escape the three characters that could break out of the XML-ish framing.

    Knowledge base documents are trusted here, but escaping is cheap and it is the
    right default for the day this pipeline ingests customer-supplied content.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
