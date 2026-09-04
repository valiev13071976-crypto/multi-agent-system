"""Immutable task routing requirements — independent of provider/model selection.

Contract only: describes what a task needs. Does not select providers, score
quality, estimate cost, or apply health/eval logic.
"""

from __future__ import annotations

import re
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
FRESHNESS_HISTORICAL = "historical"
FRESHNESS_LEVELS = (FRESHNESS_STATIC, FRESHNESS_CURRENT, FRESHNESS_HISTORICAL)

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

_HISTORICAL_RE = re.compile(
    r"(в\s+20\d{2}\s*год|в\s+20\d{2}|были\s+в\s+20\d{2}|"
    r"историческ|as\s+of\s+20\d{2}|in\s+20\d{2}|"
    r"ориентир(?:ы|а)?\s+20\d{2})",
    re.I,
)
_MARKETPLACE_RE = re.compile(
    r"(ozon|озон|wildberries|вайлдберриз|\bwb\b|маркетплейс|"
    r"yandex\s+market|яндекс\s+маркет)",
    re.I,
)
_COMMERCIAL_RATE_RE = re.compile(
    r"(комисс|тариф|логист|cpm|реклам|ставк\w*\s+(?:ндс|налог)|"
    r"правил(?:а|ах)\s+площадк)",
    re.I,
)
_MARKETPLACE_DECISION_RE = re.compile(
    r"(сравни|стратег|выбер|выход(?:а|е)\s+на)",
    re.I,
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


def _text_indicates_historical(text: str) -> bool:
    return bool(_HISTORICAL_RE.search(text or ""))


def _text_indicates_time_sensitive_commercial(text: str) -> bool:
    """Marketplace rates/policies or marketplace go-to-market decisions need current data."""
    raw = text or ""
    if _COMMERCIAL_RATE_RE.search(raw) and (
        _MARKETPLACE_RE.search(raw) or _text_indicates_current_freshness(raw)
    ):
        return True
    if _MARKETPLACE_RE.search(raw) and _MARKETPLACE_DECISION_RE.search(raw):
        return True
    return False


def resolve_freshness(text: str, *, category_default: str) -> str:
    """Temporal claim mode. Independent of response_depth / verbosity.

    HISTORICAL wins over topic-current when the user names a past period
    and does not also ask for current figures.
    """
    historical = _text_indicates_historical(text)
    wants_current = _text_indicates_current_freshness(text)
    time_sensitive = _text_indicates_time_sensitive_commercial(text)
    if historical and not wants_current:
        return FRESHNESS_HISTORICAL
    if wants_current or time_sensitive:
        return FRESHNESS_CURRENT
    if category_default in FRESHNESS_LEVELS:
        return category_default
    return FRESHNESS_STATIC


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
        if resolve_freshness(text, category_default=FRESHNESS_STATIC) == FRESHNESS_CURRENT:
            freshness = FRESHNESS_CURRENT
            caps.append(CAPABILITY_SEARCH)
    elif category_key == "trend_analysis":
        complexity = COMPLEXITY_STANDARD
        risk = RISK_MEDIUM
        # Freshness default is current, but do not hard-require CAPABILITY_SEARCH:
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

    freshness = resolve_freshness(text, category_default=freshness)

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
