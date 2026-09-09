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
