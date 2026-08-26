"""Deterministic ModelProfile ↔ TaskRequirements capability matching.

Search is intentionally unresolved: ModelProfile has no dedicated
``supports_search`` (or equivalent) field. Matching never treats
``supports_tools=true`` as web-search ability. A required ``search``
capability therefore cannot be satisfied by any current profile.
"""

from __future__ import annotations

from agents.model_profile import ModelProfile
from agents.routing_requirements import (
    CAPABILITY_CODING,
    CAPABILITY_LONG_CONTEXT,
    CAPABILITY_REASONING,
    CAPABILITY_SEARCH,
    CAPABILITY_VISION,
    CONTEXT_LONG,
    TaskRequirements,
)

# Profiles with context_window at/above this are treated as long-context capable
# when context_class is not already ``long`` (matches existing profile heuristics).
LONG_CONTEXT_WINDOW_MIN = 100_000

# Capabilities with no trustworthy profile representation.
UNRESOLVED_CAPABILITIES = frozenset({CAPABILITY_SEARCH})

MATCH_PASS = "pass"
MATCH_FAIL = "fail"
MATCH_UNRESOLVED = "unresolved"


def profile_supports_long_context(profile: ModelProfile | None) -> bool:
    """Deterministic long-context check from existing profile metadata only."""

    if profile is None:
        return False
    if getattr(profile, "context_class", None) == CONTEXT_LONG:
        return True
    window = getattr(profile, "context_window", None)
    if window is not None:
        try:
            return int(window) >= LONG_CONTEXT_WINDOW_MIN
        except (TypeError, ValueError):
            return False
    return False


def match_capability(profile: ModelProfile | None, capability: str) -> str:
    """Return pass | fail | unresolved for one required capability token."""

    cap = str(capability or "").strip()
    if not cap:
        return MATCH_PASS
    if cap in UNRESOLVED_CAPABILITIES:
        # Documented: search is not represented on ModelProfile; never invent.
        return MATCH_UNRESOLVED
    if profile is None:
        return MATCH_FAIL
    if cap == CAPABILITY_CODING:
        return MATCH_PASS if profile.supports_coding else MATCH_FAIL
    if cap == CAPABILITY_REASONING:
        return MATCH_PASS if profile.supports_reasoning else MATCH_FAIL
    if cap == CAPABILITY_VISION:
        return MATCH_PASS if profile.supports_vision else MATCH_FAIL
    if cap == CAPABILITY_LONG_CONTEXT:
        return MATCH_PASS if profile_supports_long_context(profile) else MATCH_FAIL
    # Unknown capability tokens are not inventable → unresolved (not silent pass).
    return MATCH_UNRESOLVED


def missing_capabilities(
    profile: ModelProfile | None,
    requirements: TaskRequirements | None,
) -> tuple[str, ...]:
    """Capabilities that fail or are unresolved (cannot be claimed as supported)."""

    if requirements is None:
        return ()
    missing: list[str] = []
    for cap in requirements.required_capabilities:
        status = match_capability(profile, cap)
        if status in {MATCH_FAIL, MATCH_UNRESOLVED}:
            missing.append(cap)
    if requirements.context_requirement == CONTEXT_LONG:
        if not profile_supports_long_context(profile):
            if CAPABILITY_LONG_CONTEXT not in missing:
                missing.append(CAPABILITY_LONG_CONTEXT)
    # Deduplicate preserving order.
    seen = set()
    ordered = []
    for cap in missing:
        if cap not in seen:
            seen.add(cap)
            ordered.append(cap)
    return tuple(ordered)


def profile_satisfies_requirements(
    profile: ModelProfile | None,
    requirements: TaskRequirements | None,
) -> bool:
    """True iff profile satisfies every enforceable requirement (no unresolved gaps)."""

    if requirements is None:
        return True
    if not requirements.required_capabilities and requirements.context_requirement != CONTEXT_LONG:
        return True
    return not missing_capabilities(profile, requirements)
