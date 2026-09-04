"""Answer presentation policy — derived from response_depth, not task intent.

Verbosity/style only. Does not classify tasks, capabilities, or freshness.
"""

from __future__ import annotations

from agents.response_depth import (
    DEPTH_ANALYTICAL,
    DEPTH_DEEP,
    DEPTH_DIRECT,
    DEPTH_NORMAL,
    normalize_response_depth,
)

PRESENTATION_COMPACT_DIRECT = "compact_direct"
PRESENTATION_COMPACT_NORMAL = "compact_normal"
PRESENTATION_BOUNDED_ANALYTICAL = "bounded_analytical"
PRESENTATION_DETAILED_DEEP = "detailed_deep"

PRESENTATION_POLICIES = (
    PRESENTATION_COMPACT_DIRECT,
    PRESENTATION_COMPACT_NORMAL,
    PRESENTATION_BOUNDED_ANALYTICAL,
    PRESENTATION_DETAILED_DEEP,
)

_DEPTH_TO_PRESENTATION = {
    DEPTH_DIRECT: PRESENTATION_COMPACT_DIRECT,
    DEPTH_NORMAL: PRESENTATION_COMPACT_NORMAL,
    DEPTH_ANALYTICAL: PRESENTATION_BOUNDED_ANALYTICAL,
    DEPTH_DEEP: PRESENTATION_DETAILED_DEEP,
}


def presentation_policy_for(response_depth: str | None) -> str:
    """Unknown/invalid depth fails safe to compact NORMAL, never DEEP."""
    depth = normalize_response_depth(response_depth)
    return _DEPTH_TO_PRESENTATION.get(depth, PRESENTATION_COMPACT_NORMAL)
