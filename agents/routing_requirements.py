"""Immutable task routing requirements — independent of provider/model selection.

Contract only: describes what a task needs. Does not select providers, score
quality, estimate cost, or apply health/eval logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


# --- Complexity -------------------------------------------------------------

COMPLEXITY_SIMPLE = "simple"
COMPLEXITY_STANDARD = "standard"
COMPLEXITY_COMPLEX = "complex"
COMPLEXITY_LEVELS = (
    COMPLEXITY_SIMPLE,
    COMPLEXITY_STANDARD,
    COMPLEXITY_COMPLEX,
)

# --- Freshness --------------------------------------------------------------

FRESHNESS_STATIC = "static"
FRESHNESS_CURRENT = "current"
FRESHNESS_LEVELS = (FRESHNESS_STATIC, FRESHNESS_CURRENT)

# --- Risk -------------------------------------------------------------------

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_LEVELS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)

# --- Context ----------------------------------------------------------------

CONTEXT_STANDARD = "standard"
CONTEXT_LONG = "long"
CONTEXT_LEVELS = (CONTEXT_STANDARD, CONTEXT_LONG)

# --- Capability tokens (machine-readable; not provider ids) -----------------

CAPABILITY_CODING = "coding"
CAPABILITY_REASONING = "reasoning"
CAPABILITY_VISION = "vision"
CAPABILITY_SEARCH = "search"
CAPABILITY_LONG_CONTEXT = "long_context"
KNOWN_CAPABILITIES = frozenset(
    {
        CAPABILITY_CODING,
        CAPABILITY_REASONING,
        CAPABILITY_VISION,
        CAPABILITY_SEARCH,
        CAPABILITY_LONG_CONTEXT,
    }
)

# Safe threshold: only mark long context when prompt is clearly large.
LONG_CONTEXT_CHAR_THRESHOLD = 50_000

CURRENT_FRESHNESS_MARKERS = (
    "сейчас",
    "сегодня",
    "актуальн",
    "на данный момент",
    "свежие данные",
    "текущ",
    "latest",
    "current",
    "recent",
    "up to date",
    "up-to-date",
    "right now",
)


@dataclass(frozen=True)
class TaskRequirements:
    """What a task requires — never contains provider/model ids."""

    complexity: str = COMPLEXITY_SIMPLE
    freshness: str = FRESHNESS_STATIC
    risk: str = RISK_LOW
    required_capabilities: tuple[str, ...] = ()
    context_requirement: str = CONTEXT_STANDARD

    def __post_init__(self):
        complexity = self.complexity if self.complexity in COMPLEXITY_LEVELS else COMPLEXITY_SIMPLE
        freshness = self.freshness if self.freshness in FRESHNESS_LEVELS else FRESHNESS_STATIC
        risk = self.risk if self.risk in RISK_LEVELS else RISK_LOW
        context = (
            self.context_requirement
            if self.context_requirement in CONTEXT_LEVELS
            else CONTEXT_STANDARD
        )
        caps = tuple(
            cap
            for cap in tuple(self.required_capabilities or ())
            if cap in KNOWN_CAPABILITIES
        )
        # Deduplicate preserving order.
        seen = set()
        ordered = []
        for cap in caps:
            if cap not in seen:
                seen.add(cap)
                ordered.append(cap)
        object.__setattr__(self, "complexity", complexity)
        object.__setattr__(self, "freshness", freshness)
        object.__setattr__(self, "risk", risk)
        object.__setattr__(self, "required_capabilities", tuple(ordered))
        object.__setattr__(self, "context_requirement", context)

    def as_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "complexity": self.complexity,
                "freshness": self.freshness,
                "risk": self.risk,
                "required_capabilities": self.required_capabilities,
                "context_requirement": self.context_requirement,
            }
        )


def conservative_default_requirements() -> TaskRequirements:
    """Deterministic conservative defaults for unknown/simple tasks."""

    return TaskRequirements()


def _text_indicates_current_freshness(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in CURRENT_FRESHNESS_MARKERS)


def _metadata_requires_vision(metadata: Mapping | None) -> bool:
    if not metadata:
        return False
    for key in ("requires_vision", "has_image", "has_images", "vision"):
        value = metadata.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _metadata_requires_long_context(metadata: Mapping | None) -> bool:
    if not metadata:
        return False
    raw = metadata.get("context_requirement")
    if isinstance(raw, str) and raw.strip().lower() == CONTEXT_LONG:
        return True
    if metadata.get("requires_long_context") is True:
        return True
    return False


def derive_task_requirements(
    *,
    category: str,
    text: str = "",
    metadata: Mapping | None = None,
) -> TaskRequirements:
    """Deterministic requirements from category + safe text/metadata signals."""

    category_key = str(category or "").strip() or "general"
    caps: list[str] = []
    complexity = COMPLEXITY_SIMPLE
    freshness = FRESHNESS_STATIC
    risk = RISK_LOW
    context = CONTEXT_STANDARD

    if category_key == "technical":
        complexity = COMPLEXITY_STANDARD
        risk = RISK_MEDIUM
        caps.append(CAPABILITY_CODING)
    elif category_key == "research":
        complexity = COMPLEXITY_STANDARD
        risk = RISK_LOW
        if _text_indicates_current_freshness(text):
            freshness = FRESHNESS_CURRENT
            caps.append(CAPABILITY_SEARCH)
    elif category_key == "trend_analysis":
        complexity = COMPLEXITY_STANDARD
        risk = RISK_MEDIUM
        # Freshness is current, but do not hard-require CAPABILITY_SEARCH:
        # ModelProfile has no supports_search, so search is always unresolved
        # and would 503 every trend_agent / trend_analysis route.
        freshness = FRESHNESS_CURRENT
    elif category_key == "critique":
        complexity = COMPLEXITY_STANDARD
        risk = RISK_MEDIUM
        caps.append(CAPABILITY_REASONING)
    elif category_key == "strategy":
        complexity = COMPLEXITY_STANDARD
        risk = RISK_MEDIUM
        caps.append(CAPABILITY_REASONING)
    else:
        # general / unknown → conservative defaults
        complexity = COMPLEXITY_SIMPLE
        freshness = FRESHNESS_STATIC
        risk = RISK_LOW

    # Freshness is independent of response depth and of category-default complexity.
    # Do not hard-require CAPABILITY_SEARCH here: most ModelProfiles have
    # supports_search=False and would 503 auto routing (same as trend_analysis).
    if _text_indicates_current_freshness(text) and freshness == FRESHNESS_STATIC:
        freshness = FRESHNESS_CURRENT

    if _metadata_requires_vision(metadata):
        caps.append(CAPABILITY_VISION)

    if _metadata_requires_long_context(metadata) or len(text or "") >= LONG_CONTEXT_CHAR_THRESHOLD:
        context = CONTEXT_LONG
        caps.append(CAPABILITY_LONG_CONTEXT)

    return TaskRequirements(
        complexity=complexity,
        freshness=freshness,
        risk=risk,
        required_capabilities=tuple(caps),
        context_requirement=context,
    )
