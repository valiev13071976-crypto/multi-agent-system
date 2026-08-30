"""Stable side-effect idempotency keys for retry / resume / recovery.

Same logical external write must reuse the same key across attempts.
Random UUIDs must not be minted per attempt for one logical mutation.
"""

from __future__ import annotations


def stable_side_effect_idempotency_key(
    *,
    workflow_id: str,
    tool_id: str,
    operation: str,
    resource: str = "",
    logical_suffix: str = "",
) -> str:
    """Deterministic key for a single logical protected write."""

    parts = [
        "se",
        str(workflow_id or "").strip() or "wf",
        str(tool_id or "").strip() or "tool",
        str(operation or "").strip() or "op",
        str(resource or "").strip() or "-",
    ]
    suffix = str(logical_suffix or "").strip()
    if suffix:
        parts.append(suffix)
    return ":".join(parts)
