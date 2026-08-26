"""Mistral AI provider versions and trusted endpoint constants.

Model IDs are config-driven (MISTRAL_DEFAULT_MODEL / MISTRAL_MODEL).
Documented examples from official Mistral docs (api.mistral.ai).
No model id is hard-wired as an eternal runtime default.
"""

from __future__ import annotations

import os

MISTRAL_PROVIDER_ID = "mistral"
MISTRAL_PROVIDER_ADAPTER_VERSION = "1.0.0"
MISTRAL_MODEL_REGISTRY_VERSION = "1.0.0"

# Official OpenAI-compatible base. Agent cannot override to untrusted hosts.
MISTRAL_DEFAULT_BASE_URL = "https://api.mistral.ai/v1"
MISTRAL_TRUSTED_BASE_URLS = frozenset(
    {
        "https://api.mistral.ai/v1",
    }
)

# Documented aliases from Mistral docs / models overview — catalog only, not auto-selected.
# General flagship alias: mistral-large-latest
# Coding-specialized alias: codestral-latest
# Also documented: mistral-medium-latest, mistral-small-latest
MISTRAL_DOCUMENTED_MODEL_EXAMPLES = (
    "mistral-large-latest",
    "mistral-medium-latest",
    "mistral-small-latest",
    "codestral-latest",
)

MISTRAL_RECOMMENDED_GENERAL_MODEL = "mistral-large-latest"
MISTRAL_RECOMMENDED_CODING_MODEL = "codestral-latest"

# Do NOT invent a default temperature for chat requests.
# Live diagnosis: sending temperature=0.7 caused ReadTimeout for mistral-large-latest;
# omitting temperature succeeds. Optional temperature only when explicitly configured.
def resolve_mistral_chat_temperature(env: dict | None = None) -> float | None:
    """Return explicit temperature if configured; otherwise None (omit from request)."""

    source = env if env is not None else os.environ
    raw = source.get("MISTRAL_TEMPERATURE")
    if raw is None or not str(raw).strip():
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


QUALITY_STATUS_PROVISIONAL = "provisional"
QUALITY_STATUS_VERIFIED = "verified"
QUALITY_STATUSES = (QUALITY_STATUS_PROVISIONAL, QUALITY_STATUS_VERIFIED)

MODEL_STATE_ACTIVE = "active"
MODEL_STATE_DEPRECATED = "deprecated"
MODEL_STATE_DISABLED = "disabled"
MODEL_STATES = (MODEL_STATE_ACTIVE, MODEL_STATE_DEPRECATED, MODEL_STATE_DISABLED)

PRICING_STATUS_UNKNOWN = "unknown"
PRICING_STATUS_VERIFIED = "verified"
