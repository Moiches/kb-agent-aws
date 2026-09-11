"""Tunables for the query path, all overridable by environment variable.

Every value here is a knob a reviewer might want to turn without redeploying code, so
none of them are hard-coded at their call sites. The CDK stack sets these on the Lambda.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------- models
# Provider is a deployment-time choice (ADR-09). The AMCRO sandbox has no Bedrock access,
# so OpenRouter is the default and the only path exercised end to end.
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "openrouter")
MODEL_ID = os.environ.get("MODEL_ID", "anthropic/claude-haiku-4.5")
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "openai/text-embedding-3-small")
EMBED_DIMENSIONS = int(os.environ.get("EMBED_DIMENSIONS", "512"))

# The Lambda receives the ARN, never the value. Reading it at cold start and caching for
# PROVIDER_KEY_TTL_SECONDS is what lets the key be rotated without a redeploy.
PROVIDER_API_KEY_SECRET_ARN = os.environ.get("PROVIDER_API_KEY_SECRET_ARN", "")
PROVIDER_KEY_TTL_SECONDS = int(os.environ.get("PROVIDER_KEY_TTL_SECONDS", "300"))

# ------------------------------------------------------------------------- retrieval
TOP_K_DEFAULT = int(os.environ.get("TOP_K_DEFAULT", "5"))
TOP_K_MAX = int(os.environ.get("TOP_K_MAX", "10"))
MAX_CHUNKS_PER_DOC = int(os.environ.get("MAX_CHUNKS_PER_DOC", "3"))

# The flat cap above is a floor, not the whole rule. A document of `n` chunks may contribute
# up to max(MAX_CHUNKS_PER_DOC, ceil(MAX_CHUNKS_PER_DOC_RATIO * n)) hits to one search, never
# more than top_k. The ratio exists because a constant calibrated on eight short documents
# stopped being right the moment a 174-chunk paper joined the corpus: three passages were
# under 2% of the only document that could answer, and raising top_k could not help because
# the cap, not top_k, was what bound (EVALUATION.md, failure B, measured 2026-09-08).
#
# 0.05 is chosen so the current corpus is untouched: ceil(0.05 * n) <= 3 for every n <= 60,
# and the whole corpus is 52 chunks across eight documents, so retrieval on the evaluation
# set is byte-identical to the flat cap. The paper gets ceil(0.05 * 174) = 9 of 10 slots at
# top_k 10. Zero restores the flat cap.
MAX_CHUNKS_PER_DOC_RATIO = float(os.environ.get("MAX_CHUNKS_PER_DOC_RATIO", "0.05"))

# Cosine similarity below RELEVANCE_FLOOR means "we found nothing relevant": the query
# short-circuits to an abstention without paying for a generation call.
#
# CALIBRATED 2026-09-08 against the 11-question evaluation set, not guessed. Measured with
# openai/text-embedding-3-small at 512 dimensions:
#
#   answerable questions (n=10):  top_score 0.538 .. 0.775, median 0.680
#   unanswerable question (n=1):  top_score 0.326
#
# The two groups are separated by 0.21, so the floor sits at 0.40 -- clear of the
# unanswerable case with margin, and 0.14 below the weakest legitimate question. It is
# deliberately closer to the abstention case than to the midpoint: with only one negative
# sample, the cheaper mistake is answering something marginal, not refusing something
# answerable. A false refusal is the most expensive failure an enterprise assistant makes.
#
# The previous value of 0.30 was inherited from a guess made for a different embedding
# model. It was low enough that it never fired: the HIPAA question at 0.326 passed straight
# through it and was caught only by the model emitting INSUFFICIENT_CONTEXT. The floor was
# not doing its job.
RELEVANCE_FLOOR = float(os.environ.get("RELEVANCE_FLOOR", "0.40"))

# The score treated as "as good as it gets". At 0.75 two of ten questions saturated at
# strength 1.0, which throws away the ability to distinguish an excellent match from a
# merely good one. 0.80 sits just above the observed maximum of 0.775.
CONFIDENCE_CEIL = float(os.environ.get("CONFIDENCE_CEIL", "0.80"))

# ------------------------------------------------------------------------ generation
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "512"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.0"))
TOP_P = float(os.environ.get("TOP_P", "0.9"))

# ---------------------------------------------------------------------- verification
# The second reading (verifier.py): a fact-checker reads the answer against the passages it
# cites and can send it back for one revision. Off means the answered path is main's path
# plus a `verification.verdict` of "skipped" in the metadata, which is how the experiment's
# packaging-only configuration (L0) is produced from the same deployment.
VERIFY_ENABLED = os.environ.get("VERIFY_ENABLED", "true").lower() == "true"

# The checker's slug. Defaults to the answer writer's model because the probe that gated
# this branch was run on it; a stronger model is one environment variable away and goes
# through the same client, retries and error translation (the `model` override on
# `generate`). Empty means the default, so a blank variable cannot send "" upstream.
VERIFY_MODEL_ID = os.environ.get("VERIFY_MODEL_ID") or MODEL_ID

# How many times a failed answer may be rewritten. One: the second reading is meant to catch
# a misread boundary, not to iterate towards an answer, and every round is a generation plus
# a verification against a 28 s Lambda. Zero verifies but never revises.
VERIFY_MAX_ROUNDS = int(os.environ.get("VERIFY_MAX_ROUNDS", "1"))

# Enough for the JSON the prompt asks for on a four-bullet answer. A reply that is cut off
# mid-object parses as `unverified`, never as a verdict, so a cap that is too low fails safe
# and shows up in the metadata rather than in a wrong label.
VERIFY_MAX_TOKENS = int(os.environ.get("VERIFY_MAX_TOKENS", "400"))

# The most passages the checker is shown. Only the ones the answer cites are sent, so with
# the default top_k of 5 this only bites when an answer cites everything it was given.
VERIFY_MAX_PASSAGES = int(os.environ.get("VERIFY_MAX_PASSAGES", "4"))

# Time that must remain before the Lambda deadline for a verification or a revision to
# start. One round is a generation plus a verification, each measured at 1-4 s against
# OpenRouter, plus the response assembly; below this the answer is returned as it stands
# with the verdict "skipped", because a truncated request is worse than an unverified one.
# A heuristic, not a guarantee: a single hung provider call can still exceed it, and the
# Lambda timeout remains the real backstop.
VERIFY_DEADLINE_RESERVE_MS = int(os.environ.get("VERIFY_DEADLINE_RESERVE_MS", "9000"))

# --------------------------------------------------------------------- orchestration
# Which runner drives the nodes in nodes.py: "langgraph" (graph.py, the experiment) or
# "loop" (nodes.run_loop, the same functions under a while-loop). Both produce identical
# state -- tests/test_graph.py holds them to it -- so this toggle isolates the framework's
# own cost, which is one of the numbers the experiment measures. The handler imports
# graph.py only when this says so, so "loop" never imports langgraph at all.
ORCHESTRATOR = os.environ.get("ORCHESTRATOR", "langgraph")

# --------------------------------------------------------------------------- request
# Answer styles. "simple" is the reference prototype's "Explain like I'm 10" toggle. Both
# styles share their grounding rules verbatim: the register changes, the evidence bar does not.
ALLOWED_STYLES = ("standard", "simple")
DEFAULT_STYLE = os.environ.get("DEFAULT_STYLE", "standard")

MAX_QUESTION_CHARS = int(os.environ.get("MAX_QUESTION_CHARS", "1000"))
MIN_QUESTION_CHARS = int(os.environ.get("MIN_QUESTION_CHARS", "3"))
EXCERPT_CHARS = int(os.environ.get("EXCERPT_CHARS", "280"))

# --------------------------------------------------------------------------- storage
KB_BUCKET = os.environ.get("KB_BUCKET", "")
KB_INDEX_KEY = os.environ.get("KB_INDEX_KEY", "index/kb-index.json.gz")
# How long a warm execution environment may serve its cached index without checking
# whether a re-ingest replaced it. Zero disables the check and restores load-once
# behaviour. See the reasoning in index_store.py.
INDEX_REFRESH_SECONDS = int(os.environ.get("INDEX_REFRESH_SECONDS", "60"))
QUERY_LOG_TABLE = os.environ.get("QUERY_LOG_TABLE", "")
QUERY_LOG_TTL_DAYS = int(os.environ.get("QUERY_LOG_TTL_DAYS", "30"))

# --------------------------------------------------------------------- observability
LOG_QUESTIONS = os.environ.get("LOG_QUESTIONS", "true").lower() == "true"
EMIT_METRICS = os.environ.get("EMIT_METRICS", "true").lower() == "true"
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "1.0.0")

# ------------------------------------------------------------------------------ cost
# Used only to annotate responses with a rough per-request estimate, so that token spend is
# visible in the client and in the query log rather than discovered on a bill. Rates change;
# verify at openrouter.ai/models before quoting them anywhere that matters. This is a
# debugging aid, not a billing source of truth.
COST_PER_1K_INPUT_TOKENS = float(os.environ.get("COST_PER_1K_INPUT_TOKENS", "0.001"))
COST_PER_1K_OUTPUT_TOKENS = float(os.environ.get("COST_PER_1K_OUTPUT_TOKENS", "0.005"))
