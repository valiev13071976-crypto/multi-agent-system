"""Build scale optimization runtime — reuses capacity snapshot when available."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from scale_optimization.access import ScaleOptimizationAccessPolicy
from scale_optimization.service import ScaleOptimizationService


@dataclass
class ScaleOptimizationRuntime:
    service: ScaleOptimizationService
    policy: ScaleOptimizationAccessPolicy


def build_scale_optimization_runtime(
    *,
    capacity_provider: Callable[[], Any] | None = None,
) -> ScaleOptimizationRuntime:
    policy = ScaleOptimizationAccessPolicy()
    service = ScaleOptimizationService(access=policy, capacity_provider=capacity_provider)
    return ScaleOptimizationRuntime(service=service, policy=policy)
