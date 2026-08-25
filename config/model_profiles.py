import os

from agents.model_profile import (
    AUTO_CAPABILITY_FALLBACK_ENV,
    AUTO_ROUTING_POLICY_ENV,
    PROVIDER_PROFILE_ENV,
    ModelProfile,
    build_model_profile,
    parse_auto_capability_fallback,
    parse_auto_routing_policy,
)


def load_model_profiles(records: dict) -> dict[str, ModelProfile]:
    profiles = {}
    for provider_id, record in records.items():
        prefix = PROVIDER_PROFILE_ENV[provider_id]
        profiles[provider_id] = build_model_profile(
            provider_id,
            record.model,
            task_categories_raw=os.getenv(f"{prefix}_TASK_CATEGORIES"),
            quality_raw=os.getenv(f"{prefix}_QUALITY_CLASS"),
            cost_raw=os.getenv(f"{prefix}_COST_CLASS"),
            latency_raw=os.getenv(f"{prefix}_LATENCY_CLASS"),
            context_raw=os.getenv(f"{prefix}_CONTEXT_CLASS"),
            tools_raw=os.getenv(f"{prefix}_SUPPORTS_TOOLS"),
            vision_raw=os.getenv(f"{prefix}_SUPPORTS_VISION"),
            structured_raw=os.getenv(f"{prefix}_SUPPORTS_STRUCTURED_OUTPUT"),
            enabled=True,
        )
    return profiles


def load_auto_capability_fallback() -> str:
    return parse_auto_capability_fallback(os.getenv(AUTO_CAPABILITY_FALLBACK_ENV))


def load_auto_routing_policy() -> str:
    return parse_auto_routing_policy(os.getenv(AUTO_ROUTING_POLICY_ENV))
