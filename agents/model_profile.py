from dataclasses import dataclass


QUALITY_CLASSES = ("standard", "premium")
COST_CLASSES = ("cheap", "standard", "premium")
LATENCY_CLASSES = ("fast", "standard", "slow")
CONTEXT_CLASSES = ("standard", "long")

TASK_CATEGORIES = (
    "strategy",
    "critique",
    "research",
    "trend_analysis",
    "technical",
    "general",
)
KNOWN_TASK_CATEGORIES = frozenset(TASK_CATEGORIES)

FALLBACK_GENERAL = "general"
FALLBACK_PRIORITY = "priority"
FALLBACK_ERROR = "error"
AUTO_CAPABILITY_FALLBACKS = (FALLBACK_GENERAL, FALLBACK_PRIORITY, FALLBACK_ERROR)
DEFAULT_AUTO_CAPABILITY_FALLBACK = FALLBACK_GENERAL

DEFAULT_QUALITY_CLASS = "standard"
DEFAULT_COST_CLASS = "standard"
DEFAULT_LATENCY_CLASS = "standard"
DEFAULT_CONTEXT_CLASS = "standard"
DEFAULT_TASK_CATEGORIES = ("general",)

ROLE_TO_ROUTING_CATEGORY = {
    "strategist": "strategy",
    "critic": "critique",
    "researcher": "research",
    "trend_agent": "trend_analysis",
    "technical": "technical",
}

PROVIDER_PROFILE_ENV = {
    "openai": "OPENAI",
    "anthropic": "ANTHROPIC",
    "gemini": "GEMINI",
    "grok": "XAI",
    "deepseek": "DEEPSEEK",
    "moonshot": "MOONSHOT",
    "mistral": "MISTRAL",
}

AUTO_CAPABILITY_FALLBACK_ENV = "AUTO_CAPABILITY_FALLBACK"
AUTO_ROUTING_POLICY_ENV = "AUTO_ROUTING_POLICY"

POLICY_PRIORITY = "priority"
POLICY_QUALITY = "quality"
POLICY_COST = "cost"
POLICY_LATENCY = "latency"
POLICY_BALANCED = "balanced"
AUTO_ROUTING_POLICIES = (
    POLICY_PRIORITY,
    POLICY_QUALITY,
    POLICY_COST,
    POLICY_LATENCY,
    POLICY_BALANCED,
)
DEFAULT_AUTO_ROUTING_POLICY = POLICY_PRIORITY

# Bump when ranking / selection semantics change (evals pin this).
ROUTING_POLICY_VERSION = "1.0.0"

QUALITY_RANK = {
    "premium": 2,
    "standard": 1,
}
COST_RANK = {
    "cheap": 2,
    "standard": 1,
    "premium": 0,
}
LATENCY_RANK = {
    "fast": 2,
    "standard": 1,
    "slow": 0,
}


class InvalidModelProfileError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)


class InvalidCapabilityFallbackError(ValueError):
    def __init__(self, raw: str):
        self.raw = raw
        super().__init__(
            f"Invalid {AUTO_CAPABILITY_FALLBACK_ENV}={raw!r}. "
            f"Allowed: {', '.join(AUTO_CAPABILITY_FALLBACKS)}."
        )


class InvalidAutoRoutingPolicyError(ValueError):
    def __init__(self, raw: str):
        self.raw = raw
        super().__init__(
            f"Invalid {AUTO_ROUTING_POLICY_ENV}={raw!r}. "
            f"Allowed: {', '.join(AUTO_ROUTING_POLICIES)}."
        )


@dataclass(frozen=True)
class ModelProfile:
    provider_id: str
    model_id: str
    enabled: bool
    quality_class: str
    cost_class: str
    latency_class: str
    task_categories: tuple[str, ...]
    supports_tools: bool
    supports_vision: bool
    supports_structured_output: bool
    context_class: str
    # P18 optional metadata (defaults preserve prior semantics)
    context_window: int | None = None
    quality_status: str = "provisional"
    model_state: str = "active"
    supports_reasoning: bool = False
    supports_multilingual: bool = False
    supports_coding: bool = False


def routing_category_for_role(role_id: str) -> str:
    return ROLE_TO_ROUTING_CATEGORY.get(role_id, "general")


def parse_csv_categories(raw: str | None) -> tuple[str, ...]:
    if raw is None or not str(raw).strip():
        return DEFAULT_TASK_CATEGORIES

    ordered = []
    unknown = []
    seen = set()
    for part in str(raw).split(","):
        category = part.strip()
        if not category:
            continue
        if category not in KNOWN_TASK_CATEGORIES:
            if category not in unknown:
                unknown.append(category)
            continue
        if category in seen:
            continue
        seen.add(category)
        ordered.append(category)

    if unknown:
        raise InvalidModelProfileError(
            f"Unknown task category in model profile config: {', '.join(unknown)}."
        )
    if not ordered:
        return DEFAULT_TASK_CATEGORIES
    return tuple(ordered)


def parse_class_value(raw: str | None, allowed: tuple[str, ...], default: str, label: str) -> str:
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip()
    if value not in allowed:
        raise InvalidModelProfileError(
            f"Unknown {label}={value!r}. Allowed: {', '.join(allowed)}."
        )
    return value


def parse_bool_flag(raw: str | None, default: bool, label: str) -> bool:
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise InvalidModelProfileError(
        f"Malformed boolean {label}={raw!r}. Allowed: true/false, 1/0, yes/no, on/off."
    )


def parse_auto_capability_fallback(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return DEFAULT_AUTO_CAPABILITY_FALLBACK
    value = str(raw).strip()
    if value not in AUTO_CAPABILITY_FALLBACKS:
        raise InvalidCapabilityFallbackError(raw=str(raw))
    return value


def parse_auto_routing_policy(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return DEFAULT_AUTO_ROUTING_POLICY
    value = str(raw).strip()
    if value not in AUTO_ROUTING_POLICIES:
        raise InvalidAutoRoutingPolicyError(raw=str(raw))
    return value


def balanced_score(profile: ModelProfile) -> int:
    return (
        QUALITY_RANK[profile.quality_class]
        + COST_RANK[profile.cost_class]
        + LATENCY_RANK[profile.latency_class]
    )


def build_model_profile(
    provider_id: str,
    model_id: str,
    *,
    task_categories_raw: str | None = None,
    quality_raw: str | None = None,
    cost_raw: str | None = None,
    latency_raw: str | None = None,
    context_raw: str | None = None,
    tools_raw: str | None = None,
    vision_raw: str | None = None,
    structured_raw: str | None = None,
    enabled: bool = True,
    context_window: int | None = None,
    quality_status: str = "provisional",
    model_state: str = "active",
    reasoning_raw: str | None = None,
    multilingual_raw: str | None = None,
    coding_raw: str | None = None,
) -> ModelProfile:
    prefix = PROVIDER_PROFILE_ENV[provider_id]
    status = str(quality_status or "provisional").strip().lower()
    if status not in {"provisional", "verified"}:
        status = "provisional"
    state = str(model_state or "active").strip().lower()
    if state not in {"active", "deprecated", "disabled"}:
        state = "active"
    return ModelProfile(
        provider_id=provider_id,
        model_id=model_id or "",
        enabled=enabled,
        quality_class=parse_class_value(
            quality_raw, QUALITY_CLASSES, DEFAULT_QUALITY_CLASS, f"{prefix}_QUALITY_CLASS"
        ),
        cost_class=parse_class_value(
            cost_raw, COST_CLASSES, DEFAULT_COST_CLASS, f"{prefix}_COST_CLASS"
        ),
        latency_class=parse_class_value(
            latency_raw, LATENCY_CLASSES, DEFAULT_LATENCY_CLASS, f"{prefix}_LATENCY_CLASS"
        ),
        task_categories=parse_csv_categories(task_categories_raw),
        supports_tools=parse_bool_flag(tools_raw, False, f"{prefix}_SUPPORTS_TOOLS"),
        supports_vision=parse_bool_flag(vision_raw, False, f"{prefix}_SUPPORTS_VISION"),
        supports_structured_output=parse_bool_flag(
            structured_raw, False, f"{prefix}_SUPPORTS_STRUCTURED_OUTPUT"
        ),
        context_class=parse_class_value(
            context_raw, CONTEXT_CLASSES, DEFAULT_CONTEXT_CLASS, f"{prefix}_CONTEXT_CLASS"
        ),
        context_window=context_window,
        quality_status=status,
        model_state=state,
        supports_reasoning=parse_bool_flag(
            reasoning_raw, False, f"{prefix}_SUPPORTS_REASONING"
        ),
        supports_multilingual=parse_bool_flag(
            multilingual_raw, False, f"{prefix}_SUPPORTS_MULTILINGUAL"
        ),
        supports_coding=parse_bool_flag(coding_raw, False, f"{prefix}_SUPPORTS_CODING"),
    )
