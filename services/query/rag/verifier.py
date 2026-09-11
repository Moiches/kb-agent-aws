"""Second-pass verification: a fact-checker that reads the answer against its own evidence.

`confidence.py` says plainly what it cannot see: it scores retrieval quality, not factual
correctness, so a perfect retrieval followed by a bad generation still scores high. The
evaluation set produced exactly that case on 2026-09-08. Q2 asks which service credit
applies at 99.0% uptime; the passage says "below 99.9% but at or above 99.0%" earns 10%,
and the standard-style answer read the boundary backwards, declared that no credit applied,
cited the right passage, and left with confidence 0.75 and grounding "high". Nothing in the
pipeline could disagree with it, because nothing in the pipeline reads the passages twice.

This module is the second reading. It is deliberately narrow:

*   **Claims against quotes, not answers against impressions.** The prompt demands the
    chunk_id and the exact span behind every claim, and says how to read a numeric boundary.
    A checker that only says "looks fine" is the generator marking its own work; one that
    has to produce the span is at least forced to look at it.

*   **The passages the answer cites, not the whole retrieval.** The question being asked is
    whether the answer follows from its stated evidence. Handing the checker passages the
    answer never used invites it to argue about what the answer should have said instead.
    When nothing is cited there is no stated evidence, so the top passages stand in.

*   **Per-claim statuses outrank the summary verdict.** A model asked for both will now and
    then mark a claim `contradicted` and still write `"verdict": "pass"`. The per-claim
    rows are the work; the verdict is the model's summary of it. When they disagree, the
    work wins and the verdict is `fail`.

*   **Unparsable output is `unverified`, never `pass` or `fail`.** A checker that failed to
    speak is not evidence either way. The request continues regardless, and the caller
    decides what an unverified answer is worth.

Standard library only. The same module has to run under both orchestrators on this branch
and on main if it is cherry-picked there, so it takes no dependency either could object to.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .confidence import extract_citations
from .models import Hit
from .prompt import _escape

# Verbatim from the experiment plan. This text is what scripts/probe_verifier.py measures,
# so a change here invalidates the probe result recorded against it.
VERIFIER_PROMPT = (
    "You are a strict fact-checker. For every factual claim in the ANSWER: name the "
    "chunk_id it relies on, QUOTE the exact span that supports it, and check numeric "
    "boundaries and comparison directions literally: below X excludes X, at or above X "
    "includes X, within N days includes day N. When the QUESTION names a value, state "
    "which row or sentence covers that exact value. Status is supported only if the quote "
    "supports the claim under that literal reading, contradicted if a passage says "
    "otherwise, not_found if no passage covers it. Output ONLY JSON: "
    '{"claims":[{"claim","chunk_id","status":"supported|contradicted|not_found","quote"}],'
    '"verdict":"pass|fail","issue":"one sentence, empty on pass"}'
)

# Verdicts. The first two are the model's; the last two are ours, for when the check did
# not happen (`skipped`: disabled or no time left) or happened but said nothing usable.
PASS = "pass"
FAIL = "fail"
UNVERIFIED = "unverified"
SKIPPED = "skipped"

# Per-claim statuses the prompt allows.
SUPPORTED = "supported"
CONTRADICTED = "contradicted"
NOT_FOUND = "not_found"

# The reply is asked to be bare JSON, but a fenced block is the most common deviation.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class Verdict:
    """What the checker concluded, in a shape the handler can report without translation.

    `claims` is every row the model returned, `failed_claims` the subset that did not come
    back `supported`, each a dict with `claim`, `chunk_id`, `status` and `quote`. Plain dicts
    rather than a dataclass because they go straight into `metadata.verification` and into
    the reviewer note, and neither wants another conversion.
    """

    verdict: str
    issue: str = ""
    claims: list[dict] = field(default_factory=list)
    failed_claims: list[dict] = field(default_factory=list)
    claims_checked: int = 0

    @classmethod
    def skipped(cls, reason: str) -> "Verdict":
        return cls(verdict=SKIPPED, issue=reason)

    @classmethod
    def unverified(cls, reason: str) -> "Verdict":
        return cls(verdict=UNVERIFIED, issue=reason)


def build_verify_message(question: str, answer: str, hits: list[Hit], max_passages: int) -> str:
    """Render the question, the answer and the passages it rests on as one user turn.

    Only the passages the answer cites are included, in retrieval order, so the checker
    judges the answer against its own evidence. An answer that cites nothing -- or cites
    only ids that `verify_citations` already dropped -- has no stated evidence, so the
    top-ranked passages stand in for it.

    `max_passages` bounds the checker's input whichever set is used. With the default of
    four and a `top_k` of five, only an answer that cites every retrieved passage loses one,
    and the one it loses is the lowest-scored.
    """
    cited = set(extract_citations(answer))
    chosen = [hit for hit in hits if hit.chunk.chunk_id in cited] or list(hits)
    passages = "\n".join(_render_passage(hit) for hit in chosen[:max_passages])
    return (
        f"<question>{_escape(question)}</question>\n\n"
        f"<answer>\n{_escape(answer)}\n</answer>\n\n"
        f"<passages>\n{passages}\n</passages>"
    )


def _render_passage(hit: Hit) -> str:
    """Like the generation prompt's passage, minus the score: a similarity number is
    retrieval information, and the checker should have no reason to trust a passage more
    because the embedding model liked it."""
    chunk = hit.chunk
    attributes = [f'chunk_id="{_escape(chunk.chunk_id)}"']
    if chunk.section:
        attributes.append(f'section="{_escape(chunk.section)}"')
    if chunk.page is not None:
        attributes.append(f'page="{chunk.page}"')
    return f"<passage {' '.join(attributes)}>\n{_escape(chunk.text)}\n</passage>"


def parse_verdict(text: str) -> Verdict:
    """Turn the checker's reply into a Verdict, forgiving the formatting and nothing else.

    A claim whose status is anything but `supported` fails the answer, whatever the model
    wrote in `verdict`. That covers `contradicted` and `not_found`, and it also covers a
    status the prompt never offered: a checker that treated an unrecognised status as a
    pass would be failing open, which is the one thing a checker must not do.
    """
    payload = _extract_json(text)
    if payload is None:
        return Verdict.unverified("verifier output was not a JSON object")

    claims = _normalise_claims(payload.get("claims"))
    failed = [claim for claim in claims if claim["status"] != SUPPORTED]
    stated = str(payload.get("verdict") or "").strip().lower()
    issue = str(payload.get("issue") or "").strip()

    if failed:
        verdict = FAIL
    elif stated in (PASS, FAIL):
        verdict = stated
    elif claims:
        # Every claim supported and no summary given: the rows speak for themselves.
        verdict = PASS
    else:
        return Verdict.unverified("verifier output carried neither claims nor a verdict")

    if verdict == FAIL and not issue:
        # The reviewer note quotes this sentence back to the generator, so it must exist.
        issue = (
            f"{len(failed)} of {len(claims)} claims are not supported by the cited passages"
            if failed else "the verifier reported a failure without naming a claim"
        )

    return Verdict(
        verdict=verdict,
        issue=issue,
        claims=claims,
        failed_claims=failed,
        claims_checked=len(claims),
    )


def _extract_json(text: str) -> dict | None:
    """Find the JSON object in a reply, trying the contract first and the lenient readings
    after. Returns None when there is no object to be found."""
    text = (text or "").strip()

    # 1. The contract: the whole reply is the object.
    payload = _load_object(text)
    if payload is not None:
        return payload

    # 2. The common deviation: the object inside a Markdown fence.
    fence = _FENCE_RE.search(text)
    if fence:
        payload = _load_object(fence.group(1))
        if payload is not None:
            return payload

    # 3. The last resort: the first balanced object anywhere in the reply, found by the
    #    JSON decoder itself rather than by counting braces, so a brace inside a quoted
    #    span cannot unbalance it.
    decoder = json.JSONDecoder()
    for start, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _load_object(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _normalise_claims(raw: object) -> list[dict]:
    """Coerce whatever came back under `claims` into rows with the four expected keys.

    Statuses are lower-cased and space-joined so "Not found" and "not_found" agree, and
    a trailing full stop is forgiven; anything else is left as the model wrote it and,
    per `parse_verdict`, counts as a failure.
    """
    if not isinstance(raw, list):
        return []
    claims: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().rstrip(".").lower().replace(" ", "_")
        claims.append(
            {
                "claim": str(item.get("claim") or "").strip(),
                "chunk_id": str(item.get("chunk_id") or "").strip(),
                "status": status,
                "quote": str(item.get("quote") or "").strip(),
            }
        )
    return claims
