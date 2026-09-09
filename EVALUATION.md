# Evaluation

Eleven questions run against the deployed API on 2026-09-08. Total cost: **$0.019**. Median
latency: **2.4 s**.

The set is built to fail, not to pass. Seven questions the system should answer, one it
should refuse, and three chosen because they attack a specific weakness: a negation, a
cross-document synthesis, and a question in Spanish against an English corpus. An
evaluation containing only questions the system handles proves nothing except that the
author picked the questions.

Reproduce with:

```bash
python scripts/eval_run.py --stack <stack-name>
```

The harness scores what is mechanical — which documents were retrieved, at what scores,
whether the system abstained. **Factual correctness is graded by hand.** A script that
graded its own answers would be the same model marking its own work.

---

## Results

| # | Question | Behaviour | Factually correct | Top doc | Top score | Confidence | Grounding | Cited |
|---|---|---|---|---|---|---|---|---|
| Q1 | Enterprise refund window | pass | ✅ | correct | 0.679 | 0.67 | medium | 1/5 |
| Q2 | Service credit at 99.0% uptime | pass | ❌ **wrong** | correct | 0.753 | 0.75 | high | 1/5 |
| Q3 | Where data is stored, encrypted? | pass | ✅ | correct | 0.742 | 0.86 | high | 2/5 |
| Q4 | Severity 1 response time | pass | ✅ | correct | 0.726 | 0.86 | high | 2/5 |
| Q5 | SAML SSO setup | pass | ✅ | correct | 0.775 | 0.86 | high | 2/5 |
| Q6 | Exceeding the API quota | pass | ✅ | correct | 0.681 | 0.79 | high | 2/5 |
| Q7 | On-call escalation path | pass | ✅ | correct | 0.646 | 0.63 | medium | 1/5 |
| Q8 | Refund after 45 days (negation) | pass | ✅ | correct | 0.601 | 0.58 | medium | 1/5 |
| Q9 | Refund vs service credit (2 docs) | pass | ✅ | correct | 0.538 | 0.64 | medium | 4/5 |
| Q10 | HIPAA BAAs (not in corpus) | pass | ✅ abstained | n/a | 0.326 | 0.22 | insufficient_context | 0/0 |
| Q11 | Refund policy, asked in Spanish | pass | ✅ | correct | 0.540 | 0.64 | medium | 2/5 |

**11/11 behaved correctly** (answered when they should, abstained when they should).
**10/11 were factually correct.** The right document ranked first every time.

---

## Calibration

The relevance floor and confidence ceiling were **guesses inherited from a different
embedding model**. This run is what turned them into measurements.

### The distribution

| Group | n | min | median | max |
|---|---|---|---|---|
| Should be answered | 10 | 0.538 | 0.680 | 0.775 |
| Should abstain | 1 | — | 0.326 | — |

A gap of **0.21** separates them. The floor was set to **0.40**: clear of the unanswerable
case, and 0.14 below the weakest legitimate question. Deliberately closer to the abstention
case than to the midpoint — with one negative sample, the cheaper error is answering
something marginal, not refusing something answerable.

The old floor of **0.30 never fired**. The HIPAA question scored 0.326 and passed straight
through it; the abstention came from the model emitting `INSUFFICIENT_CONTEXT`, not from the
threshold. A guard that never triggers is not a guard.

The ceiling moved **0.75 → 0.80** because two questions saturated at strength 1.0, which
throws away the ability to tell an excellent match from a merely good one.

### A defect the data exposed

`citation_coverage` divided by the number of retrieved passages — that is, by `top_k`, a
value the *caller* chooses and which has nothing to do with how many passages an answer
needs. It was punishing precision:

| | Retrieval strength | Cited | Coverage | Confidence |
|---|---|---|---|---|
| **Q2** — easy, cited the one passage that answered it | **1.00** | 1/5 | 0.20 | 0.74 |
| **Q9** — hardest question, weakest retrieval | 0.53 | 4/5 | 0.80 | 0.68 |

A question with perfect retrieval and a precise citation scored barely above the hardest
question in the set, purely because the second answer was wordier. The term now divides by
a constant (two citations is sufficient evidence of grounding), so the score no longer
depends on `top_k`. Four regression tests pin this.

### Before and after

| | Median | Range | Separation from abstention | Correlation with retrieval quality |
|---|---|---|---|---|
| Before | 0.70 | 0.58–0.77 | 0.33 | r = +0.799 |
| After | 0.71 | **0.58–0.86** | **0.36** | r = +0.813 |

The correlation barely moved (+0.014) and it would be overclaiming to present that as the
win. The real improvements are the **wider range** — the score now discriminates instead of
bunching — and the removal of a structural defect. Q3, Q4 and Q5 rose 0.09–0.12; Q9 fell
0.04, which is the correct direction for a question whose retrieval was genuinely weakest.

---

## Where it works well

**Q5 — SAML SSO setup (confidence 0.86).** Best retrieval in the set (0.775). The document
is Q&A-formatted prose with no headed subsections, so this exercises the splitter's fallback
path, and it still landed on the right passage.

**Q9 — refund versus service credit (confidence 0.64).** The distinction is stated in two
documents and in neither one completely. The `MAX_CHUNKS_PER_DOC` cap did its job: both
documents made it into the context, and the answer cited both. This was predicted to be a
weak case and it was not.

**Q11 — asked in Spanish against an English corpus (confidence 0.64).** Retrieved the right
document, answered correctly, **in Spanish**. The failure mode feared here was a false
abstention — refusing a question the system can answer — which is the most expensive
mistake an enterprise assistant makes. It did not happen.

**This result does not generalise, and later measurement showed where it breaks.** Adding a
25-page academic PDF (174 chunks, one dense subject) and asking the same question in both
languages:

| Corpus | English | Spanish | Outcome in Spanish |
|---|---|---|---|
| Short business document | 0.638 | 0.526 | answered, confidence 0.63 |
| 174-chunk academic paper | 0.668 | 0.601 | **abstained**, confidence 0.44 |

The first explanation that suggested itself — that the Spanish query lands on the wrong part
of the paper — is wrong, and comparing the retrieved sets says so:

| | passages retrieved |
|---|---|
| English (answered) | chunk-151, **chunk-84**, chunk-61, + 2 from other documents |
| Spanish (abstained) | chunk-151, chunk-61, chunk-36, + the same 2 |

**Four of five are identical.** The single difference is `chunk-84` — which is precisely the
passage the English answer cited. The cross-lingual penalty did not move the query to a
different subject; it reordered the paper's own passages by about 0.05 and pushed the one
decisive passage out.

And out of what, exactly? Not out of `top_k`. Raising it to 8 and then 10 did not bring
`chunk-84` back, because **`MAX_CHUNKS_PER_DOC = 3` is the binding constraint**: at most three
passages from any one document reach the model, so of the paper's 174 chunks only ever three
are read. A larger `top_k` just adds low-scoring passages from *other* documents — 0.28 and
falling. `chunk-84` ranked fourth within the paper in Spanish, and the cap cut it.

Confirmed by lifting the cap to 8: the same Spanish question then answers, `grounding: medium`,
listing the risks correctly in Spanish.

That cap is a good decision that stopped being right when the corpus changed. It exists so a
single long document cannot monopolise the context — exactly what Q9 needed, where the answer
lived in two documents at once. But it was calibrated against eight short business documents,
where three passages is most of what any of them has to say. Add one 174-chunk paper and the
same rule guarantees the model sees under 2% of the only document that can answer.

Three things follow, and the third is the general one:

1. `MAX_CHUNKS_PER_DOC` should scale with the document, not be a constant — something like
   `max(3, ceil(chunks_in_doc * 0.05))` capped at `top_k`.
2. The relevance floor did not decide this. The Spanish paper question scored **higher**
   (0.601) than the Spanish refund question (0.526) and still abstained, because the model
   judged the passages, not the score. **Retrieval score and answerability are not the same
   axis** — the same lesson as Q2, approached from the other side.
3. Q11 was never evidence that cross-lingual retrieval works. It was evidence that it works
   *on a corpus where every document is short*. A parameter tuned on one corpus shape is a
   claim about that corpus, not about the system.

**Q10 — HIPAA (abstained).** No sources returned, `abstain_reason` recorded. Nothing cleared
the relevance floor, so there were no near misses worth showing: displaying the security
document next to "I could not find this" would invite the reader to believe the answer came
from it.

That reasoning turned out to cover only half the cases. It was later applied to the *other*
abstention path too — where passages **were** retrieved and the model rejected them — and
there it was wrong: the model's own explanation quotes what it read while the response
claimed there was nothing to show, leaving a claim the reader cannot check. Those passages
are now returned with `cited: false`, because the retrieval is itself informative: it
usually shows the right document was found and the wrong section of it, which is a very
different problem from the document being absent.

---

## Where it fails

### Q2 — factually wrong, with high confidence

This is the most important result in the run.

> **Question:** What service credit applies if uptime drops to 99.0%?
> **Answer given:** *"no service credit applies... credits begin at 'below 99.0% but at or
> above 95.0%'"*
> **Correct answer:** 10% of the monthly fee.

The source table:

| Monthly uptime | Service credit |
|---|---|
| **Below 99.9% but at or above 99.0%** | **10% of monthly fee** |
| Below 99.0% but at or above 95.0% | 25% of monthly fee |

99.0% is both *below 99.9%* and *at or above 99.0%*. It falls in the first row. The model
skipped to the second row and inverted the boundary condition.

**Retrieval was perfect.** Top score 0.753, correct document, correct section, correctly
cited. The system reported `confidence: 0.75, grounding: high`.

This is the limitation documented in `confidence.py` before the run, now demonstrated:

> *This measures retrieval quality, not factual correctness. A perfect retrieval followed by
> a bad generation still scores high.*

No similarity-based signal can catch this. Every input to the confidence score was
excellent; the failure was reasoning over a boundary inside a correctly retrieved passage.
**A user trusting the grounding badge would be misled here** — which is precisely why the
badge says "well grounded in the retrieved sources" rather than "correct".

### A follow-up: the "explain like I'm 10" style answers Q2 correctly

Adding the `simple` answer style (the reference prototype's "Explain like I'm 10" toggle)
produced an unexpected result. Asked the same question, the two styles disagree, and the
simple one is right:

| Style | Answer | Correct? |
|---|---|---|
| `standard` | *"no service credit applies... credits begin at below 99.0%"* | ❌ |
| `simple` | *"you get **10% of your monthly fee**... Since 99.0% is right at that lower boundary, it counts"* | ✅ |

**Three runs each, at `temperature=0`. 3/3 wrong and 3/3 right.** This is reproducible, not
sampling noise.

The two prompts share their grounding rules verbatim; the only differences are register and
these two instructions in the simple variant:

> *7. Lead with the direct answer in one sentence, then explain why it works that way.*
> *9. Stay short: at most 5 short bullets or 3 short paragraphs.*

against the standard prompt's:

> *6. Be concise: at most 4 short bullets or 2 short paragraphs.*

The most plausible reading is that **"explain why it works that way" forces the model to
traverse the table row by row**, where "be concise" lets it pattern-match to the first row
that mentions 99.0% and stop. The simple answer visibly does the reasoning the standard one
skips: *"Since 99.0% is right at that lower boundary, it counts."*

Two caveats before this is over-read. It is one question — a prompt change that helps a
boundary condition may hurt elsewhere, and nothing here has been measured across the whole
set. And the confidence score was **identical (0.75) for both**, so the system had no idea
one of them was wrong. That is the same limitation as before, unchanged.

What it does suggest is a concrete, cheap experiment worth running before the "answer
verification" work in item 1 below: **re-run the full evaluation under both styles**. If the
simple style is not worse anywhere, the standard prompt's brevity instruction is costing
accuracy and should be softened.

### My predictions about difficulty were wrong

Three questions were designed as likely failures: Q8 (negation), Q9 (cross-document), Q11
(cross-lingual). **All three passed.** The one that failed was Q2, which had been classified
as a routine lookup.

Worth recording because it is the argument for having an evaluation set at all. Intuition
about where a RAG system breaks is unreliable, and the failure was not in retrieval, where
attention naturally goes, but in reasoning over material that had been retrieved correctly.

### Smaller observations

- **Confidence sits in a narrow band.** Ten answered questions span 0.58–0.86. Nothing
  scores below 0.5 while still answering, so the `low` band is currently unreachable in
  practice. Either the corpus is too uniform to produce weak-but-answerable questions, or
  the band boundaries need moving. One evaluation set is not enough to tell which.
- **One PDF chunk still carries front matter.** `remote-work-policy.pdf#chunk-0` includes
  the title and review date. Front-matter stripping is Markdown-only by design — a PDF has
  no reliable structure to key off — and inside a 631-character page chunk the dilution is
  minor, unlike the Markdown case where it was an entire chunk.
- **n = 1 for abstention.** The floor is calibrated against a single negative example. It is
  the weakest number in this document.

---

## What I would improve next

Ordered by impact against effort.

1. **Re-run the evaluation under both answer styles.** Nearly free, and the finding above
   says it might matter: the simple style answers Q2 correctly and reproducibly. If it is not
   worse anywhere else, the standard prompt is trading accuracy for brevity.
2. **Answer verification, not just retrieval confidence.** The remaining fix for Q2 --
   and the only one that would have *caught* it rather than avoided it. A second
   pass asking the model to check each claim against the cited passage would catch a
   boundary error that no similarity metric can see. Roughly doubles per-query cost
   (~$0.002 → $0.004) — an explicit trade, and worth it for a system whose value depends on
   being right.
3. **More negative examples.** Five to ten unanswerable questions instead of one, so the
   floor rests on a distribution rather than a point. Cheap, and it is the current weakest
   link in the calibration.
4. **Hybrid retrieval (BM25 + vector).** Q9 had the weakest retrieval at 0.538 because the
   distinction it asks about is never stated in one place. Lexical matching on "refund" and
   "service credit" would surface both definitions more strongly.
5. **Table-aware chunking.** Q2's failure involved reading a Markdown table. Keeping a table
   intact with its header row, rather than letting it be split by character count, would
   give the model a better-formed object to reason over.
6. **Calibrate the confidence weights against labelled data.** The 0.50/0.25/0.25 split is
   reasoned, not fitted. With enough labelled runs the weights could be regressed against
   human correctness judgements instead of argued.
7. **Feedback collection from the client.** A thumbs up/down in Streamlit writing to the
   existing DynamoDB query log builds the dataset that item 5 needs.
