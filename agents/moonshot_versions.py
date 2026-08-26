"""Moonshot / Kimi provider versions and trusted endpoint constants.

Model IDs are config-driven (MOONSHOT_DEFAULT_MODEL / MOONSHOT_MODEL).
Documented examples from Moonshot Open Platform (api.moonshot.ai): kimi-k3, kimi-k2.6.
No model id is hard-wired as an eternal runtime default.
"""

from __future__ import annotations

MOONSHOT_PROVIDER_ID = "moonshot"
MOONSHOT_PROVIDER_ADAPTER_VERSION = "1.0.0"
MOONSHOT_MODEL_REGISTRY_VERSION = "1.0.0"
PROCUREMENT_MODEL_EVAL_VERSION = "1.0.0"

# Official OpenAI-compatible bases (international / China). Agent cannot override.
MOONSHOT_DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"
MOONSHOT_TRUSTED_BASE_URLS = frozenset(
    {
        "https://api.moonshot.ai/v1",
        "https://api.moonshot.cn/v1",
    }
)

# Documented example IDs from Moonshot docs — registry catalog only, not auto-selected.
MOONSHOT_DOCUMENTED_MODEL_EXAMPLES = (
    "kimi-k3",
    "kimi-k2.6",
    "kimi-k2.7-code",
)

QUALITY_STATUS_PROVISIONAL = "provisional"
QUALITY_STATUS_VERIFIED = "verified"
QUALITY_STATUSES = (QUALITY_STATUS_PROVISIONAL, QUALITY_STATUS_VERIFIED)

MODEL_STATE_ACTIVE = "active"
MODEL_STATE_DEPRECATED = "deprecated"
MODEL_STATE_DISABLED = "disabled"
MODEL_STATES = (MODEL_STATE_ACTIVE, MODEL_STATE_DEPRECATED, MODEL_STATE_DISABLED)

PRICING_STATUS_UNKNOWN = "unknown"
PRICING_STATUS_VERIFIED = "verified"
