"""SLI/SLO evaluation with honest insufficient-data semantics."""

from __future__ import annotations

from typing import Any

from scale_optimization.config import MIN_SAMPLE_COUNT, WORKLOAD_CLASSES
from scale_optimization.models import (
    SLO_BREACHED,
    SLO_HEALTHY,
    SLO_INSUFFICIENT_DATA,
    SLO_WARNING,
    SLOEvaluation,
)


def evaluate_slo(
    *,
    metric: str,
    target: float,
    observed: float | None,
    sample_count: int,
    window_seconds: float,
    workload_class: str,
    min_samples: int = MIN_SAMPLE_COUNT,
    higher_is_better: bool = False,
    warning_ratio: float = 0.9,
) -> SLOEvaluation:
    wl = workload_class if workload_class in WORKLOAD_CLASSES else workload_class
    if sample_count < min_samples or observed is None:
        return SLOEvaluation(
            metric=metric,
            target=target,
            observed=observed,
            window_seconds=window_seconds,
            workload_class=wl,
            sample_count=sample_count,
            min_samples=min_samples,
            status=SLO_INSUFFICIENT_DATA,
            details={"reason": "insufficient_samples"},
        )

    if higher_is_better:
        if observed >= target:
            status = SLO_HEALTHY
        elif observed >= target * warning_ratio:
            status = SLO_WARNING
        else:
            status = SLO_BREACHED
    else:
        if observed <= target:
            status = SLO_HEALTHY
        elif observed <= target / max(warning_ratio, 0.01):
            status = SLO_WARNING
        else:
            status = SLO_BREACHED

    return SLOEvaluation(
        metric=metric,
        target=target,
        observed=observed,
        window_seconds=window_seconds,
        workload_class=wl,
        sample_count=sample_count,
        min_samples=min_samples,
        status=status,
        details={},
    )


def default_slo_targets() -> dict[str, dict[str, Any]]:
    """Engineering defaults — not claimed production business SLOs."""
    return {
        "interactive_latency_p95_ms": {"target": 2000.0, "workload_class": "INTERACTIVE", "higher_is_better": False},
        "interactive_queue_wait_p95_ms": {"target": 500.0, "workload_class": "INTERACTIVE", "higher_is_better": False},
        "availability_ratio": {"target": 0.99, "workload_class": "INTERACTIVE", "higher_is_better": True},
        "batch_completion_ratio": {"target": 0.95, "workload_class": "BATCH", "higher_is_better": True},
        "provider_error_ratio": {"target": 0.05, "workload_class": "NORMAL", "higher_is_better": False},
    }
