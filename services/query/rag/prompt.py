"""Prompt construction for grounded answering.

Two choices here carry most of the grounding behaviour:

*   **Two answer styles, one evidence bar.** `standard` and `simple` ("explain like I'm 10")
    share their grounding rules verbatim. The register changes; the requirement to cite and
    the requirement to abstain do not.

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

_GROUNDING_RULES = """1. Every factual claim must be supported by the CONTEXT. Never use outside knowledge.
2. Cite the chunk_id of each supporting passage inline, in square brackets, for example
   [refund-and-cancellation-policy.md#chunk-2]. Only cite chunk_ids that literally
   appear in the CONTEXT.
3. If the CONTEXT does not contain enough information to answer, reply with exactly:
   INSUFFICIENT_CONTEXT: <one sentence naming what is missing>
   Do not guess and do not answer from general knowledge.
4. If the CONTEXT contains conflicting statements, say so explicitly and cite both sources.
5. If the question asks about something the CONTEXT rules out, say so directly rather
   than restating the policy and leaving the reader to infer the answer."""

STANDARD_PROMPT = f"""You are a knowledge base assistant for Northwind Analytics.
Answer ONLY using the CONTEXT passages provided in the user message.

Rules:
{_GROUNDING_RULES}
6. Be concise: at most 4 short bullets or 2 short paragraphs. No preamble, no sign-off."""

# "Explain like I'm 10", carried over from the reference prototype.
#
# The grounding rules are shared verbatim rather than relaxed. Simplifying the *language* must
# not lower the *evidence* bar: citations are what `verify_citations` checks and what
# `citation_coverage` scores, so an uncited plain-English answer would silently degrade the
# confidence signal while looking friendlier. A simple explanation of something true is still
# useful; a simple explanation of something unverifiable is worse than none.
SIMPLE_PROMPT = f"""You are a knowledge base assistant for Northwind Analytics, explaining to someone who is
smart but completely new to this subject -- imagine a bright ten-year-old.
Answer ONLY using the CONTEXT passages provided in the user message.

Rules:
{_GROUNDING_RULES}
6. Write plainly. Short sentences. Everyday words instead of jargon, and when a term from the
   documents cannot be avoided, explain it in the same breath.
7. Lead with the direct answer in one sentence, then explain why it works that way.
8. A concrete comparison helps when an idea is abstract, but only if it is accurate. Never
   invent an example that is not supported by the CONTEXT.
9. Stay short: at most 5 short bullets or 3 short paragraphs. Simple does not mean long.
10. Keep the citations. They look formal, but they are how a reader checks you were right."""

SYSTEM_PROMPTS = {
    "standard": STANDARD_PROMPT,
    "simple": SIMPLE_PROMPT,
}

DEFAULT_STYLE = "standard"


def system_prompt(style: str = DEFAULT_STYLE) -> str:
    """The system prompt for an answer style. Unknown styles fall back to standard."""
    return SYSTEM_PROMPTS.get(style, STANDARD_PROMPT)


def build_user_message(question: str, hits: list[Hit], reviewer_note: str | None = None) -> str:
    """Render the retrieved passages plus the question into a single user turn.

    `reviewer_note` is what the verification pass found wrong with a previous answer
    (nodes.py, `revise`). It is appended as its own tag, after the question, only when
    present: the first generation of every request sends exactly the message main sends,
    so retrieval and the first answer stay comparable across the two stacks. It is escaped
    like everything else because it quotes the checker's output, which quotes the model's.
    """
    passages = "\n".join(_render_passage(hit) for hit in hits)
    message = (
        f"<context>\n{passages}\n</context>\n\n"
        f"<question>{_escape(question)}</question>"
    )
    if reviewer_note:
        message += f"\n\n<reviewer_note>{_escape(reviewer_note)}</reviewer_note>"
    return message


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
