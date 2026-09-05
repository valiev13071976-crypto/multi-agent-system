"""Governed execution cost policy — independent of response_depth presentation.

Does not classify tasks. Does not replace freshness or capabilities.
Uncertain → STANDARD or FULL, never LIGHTWEIGHT.
"""

from __future__ import annotations

from agents.response_depth import (
    DEPTH_ANALYTICAL,
    DEPTH_DEEP,
    DEPTH_DIRECT,
    DEPTH_NORMAL,
    normalize_response_depth,
)
from agents.routing_requirements import (
    CAPABILITY_SEARCH,
    FRESHNESS_CURRENT,
    RISK_HIGH,
    TaskRequirements,
)

LATENCY_STAGE_KEYS = (
    "request_total_ms",
    "history_load_ms",
    "follow_up_resolution_ms",
    "routing_ms",
    "orchestration_ms",
    "provider_ms",
    "validation_ms",
    "judge_ms",
    "format_ms",
    "persistence_ms",
)

POLICY_LIGHTWEIGHT = "lightweight_governed"
POLICY_STANDARD = "standard_governed"
POLICY_FULL = "full_governed"

EXECUTION_POLICIES = (POLICY_LIGHTWEIGHT, POLICY_STANDARD, POLICY_FULL)

_SPECIALIST_CATEGORIES = frozenset(
    {"strategy", "critique", "research", "trend_analysis", "technical"}
)


def resolve_execution_policy(
    *,
    category: str | None,
    response_depth: str | None,
    requirements: TaskRequirements | None = None,
    follow_up_kind: str | None = None,
) -> str:
    """Choose orchestration cost. Fail toward more governance when unsure."""
    cat = str(category or "").strip() or "general"
    depth = normalize_response_depth(response_depth)
    req = requirements
    caps = tuple(getattr(req, "required_capabilities", ()) or ())
    freshness = str(getattr(req, "freshness", "") or "")

    if freshness == FRESHNESS_CURRENT:
        return POLICY_FULL
    if CAPABILITY_SEARCH in caps:
        return POLICY_FULL
    if str(getattr(req, "risk", "") or "") == RISK_HIGH:
        return POLICY_FULL
    if cat in _SPECIALIST_CATEGORIES:
        return POLICY_FULL
    if depth == DEPTH_DEEP:
        return POLICY_FULL
    if caps:
        # Tools/capabilities required — never the lightweight conversational skip.
        return POLICY_FULL if depth == DEPTH_ANALYTICAL else POLICY_STANDARD
    if depth == DEPTH_ANALYTICAL:
        return POLICY_STANDARD
    if cat != "general":
        return POLICY_STANDARD
    if depth in {DEPTH_DIRECT, DEPTH_NORMAL}:
        return POLICY_LIGHTWEIGHT
    return POLICY_STANDARD


def sanitize_latency_ms(raw) -> dict[str, int]:
    """Keep only integer stage durations. Never copy prompt/answer/secret strings."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    allowed = set(LATENCY_STAGE_KEYS)
    for key, value in raw.items():
        name = str(key)
        if name not in allowed:
            continue
        try:
            out[name] = int(value)
        except (TypeError, ValueError):
            continue
    return out
