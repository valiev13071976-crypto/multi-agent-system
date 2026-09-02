"""Before/after optimization evidence — comparable workloads only."""

from __future__ import annotations

from typing import Any

from scale_optimization.errors import INVALID_COMPARISON, ScaleOptimizationError
from scale_optimization.models import OptimizationEvidence


def compare_before_after(
    *,
    path: str,
    before: dict[str, Any],
    after: dict[str, Any],
    change: str,
    workload_profile: str,
    correctness_ok: bool = True,
    primary_metric: str = "latency.p95",
) -> OptimizationEvidence:
    if before.get("profile") and after.get("profile") and before["profile"] != after["profile"]:
        raise ScaleOptimizationError(INVALID_COMPARISON, "workload_profile_mismatch")
    if before.get("workload_class") and after.get("workload_class") and before["workload_class"] != after["workload_class"]:
        raise ScaleOptimizationError(INVALID_COMPARISON, "workload_class_mismatch")

    def _get(d: dict[str, Any], dotted: str) -> float | None:
        cur: Any = d
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        try:
            return float(cur)
        except (TypeError, ValueError):
            return None

    b = _get(before, primary_metric)
    a = _get(after, primary_metric)
    improvement = False
    notes = ""
    if b is None or a is None:
        notes = "primary_metric_missing"
    elif a < b:
        improvement = True
        notes = f"{primary_metric} improved {b} -> {a}"
    else:
        notes = f"{primary_metric} not improved {b} -> {a}"

    return OptimizationEvidence(
        path=path,
        before=dict(before),
        change=change,
        after=dict(after),
        workload_profile=workload_profile,
        improvement=improvement and correctness_ok,
        correctness_ok=correctness_ok,
        notes=notes,
    )
