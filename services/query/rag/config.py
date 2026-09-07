"""Tunables for the query path, all overridable by environment variable.

Every value here is a knob a reviewer might want to turn without redeploying code, so
none of them are hard-coded at their call sites. The CDK stack sets these on the Lambda.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------- models
MODEL_ID = os.environ.get("MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMENSIONS = int(os.environ.get("EMBED_DIMENSIONS", "512"))

# ------------------------------------------------------------------------- retrieval
TOP_K_DEFAULT = int(os.environ.get("TOP_K_DEFAULT", "5"))
TOP_K_MAX = int(os.environ.get("TOP_K_MAX", "10"))
MAX_CHUNKS_PER_DOC = int(os.environ.get("MAX_CHUNKS_PER_DOC", "3"))

# Cosine similarity below RELEVANCE_FLOOR means "we found nothing relevant": the query
# short-circuits to an abstention without paying for a generation call.
#
# NOTE: these two constants are an initial hypothesis, not a measured result. Titan V2
# cosine scores on related text usually land between 0.4 and 0.8, but that depends on
# the corpus. They must be calibrated against the evaluation set (see EVALUATION.md)
# before they can be described as tuned.
RELEVANCE_FLOOR = float(os.environ.get("RELEVANCE_FLOOR", "0.30"))
CONFIDENCE_CEIL = float(os.environ.get("CONFIDENCE_CEIL", "0.75"))

# ------------------------------------------------------------------------ generation
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "512"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.0"))
TOP_P = float(os.environ.get("TOP_P", "0.9"))

# --------------------------------------------------------------------------- request
MAX_QUESTION_CHARS = int(os.environ.get("MAX_QUESTION_CHARS", "1000"))
MIN_QUESTION_CHARS = int(os.environ.get("MIN_QUESTION_CHARS", "3"))
EXCERPT_CHARS = int(os.environ.get("EXCERPT_CHARS", "280"))

# --------------------------------------------------------------------------- storage
KB_BUCKET = os.environ.get("KB_BUCKET", "")
KB_INDEX_KEY = os.environ.get("KB_INDEX_KEY", "index/kb-index.json.gz")
QUERY_LOG_TABLE = os.environ.get("QUERY_LOG_TABLE", "")
QUERY_LOG_TTL_DAYS = int(os.environ.get("QUERY_LOG_TTL_DAYS", "30"))

# --------------------------------------------------------------------- observability
LOG_QUESTIONS = os.environ.get("LOG_QUESTIONS", "true").lower() == "true"
EMIT_METRICS = os.environ.get("EMIT_METRICS", "true").lower() == "true"
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "1.0.0")

# ------------------------------------------------------------------------------ cost
# Published Bedrock on-demand rates, used only to annotate responses with an estimate.
# Verify against https://aws.amazon.com/bedrock/pricing/ before quoting these anywhere
# that matters; they are a debugging aid, not a billing source of truth.
COST_PER_1K_INPUT_TOKENS = float(os.environ.get("COST_PER_1K_INPUT_TOKENS", "0.00025"))
COST_PER_1K_OUTPUT_TOKENS = float(os.environ.get("COST_PER_1K_OUTPUT_TOKENS", "0.00125"))
