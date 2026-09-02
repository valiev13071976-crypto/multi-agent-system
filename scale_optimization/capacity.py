"""Capacity model — workload-aware, fails honest on insufficient data."""

from __future__ import annotations

from typing import Any

from scale_optimization.config import MIN_SAMPLE_COUNT, WORKLOAD_INTERACTIVE
from scale_optimization.models import (
    BN_PROVIDER,
    BN_QUEUE,
    BN_WORKER_POOL,
    CAP_HEALTHY,
    CAP_INSUFFICIENT_DATA,
    CAP_NEAR_CAPACITY,
    CAP_OVERLOADED,
    CAP_SATURATED,
    CapacityState,
)


def evaluate_capacity(
    *,
    workload_class: str = WORKLOAD_INTERACTIVE,
    arrival_rate: float | None = None,
    completion_rate: float | None = None,
    queue_depth: int | None = None,
    oldest_age_seconds: float | None = None,
    active_concurrency: int | None = None,
    max_concurrency: int | None = None,
    provider_saturation: float | None = None,
    rejection_count: int | None = None,
    sample_count: int = 0,
    min_samples: int = MIN_SAMPLE_COUNT,
) -> CapacityState:
    if sample_count < min_samples and (
        arrival_rate is None or completion_rate is None or active_concurrency is None
    ):
        return CapacityState(
            state=CAP_INSUFFICIENT_DATA,
            arrival_rate=arrival_rate,
            completion_rate=completion_rate,
            queue_growth=None,
            active_concurrency=active_concurrency,
            available_concurrency=None,
            worker_saturation=None,
            provider_saturation=provider_saturation,
            headroom=None,
            bottleneck_candidate=None,
            workload_class=workload_class,
            evidence={"reason": "insufficient_samples", "sample_count": sample_count},
        )

    queue_growth = None
    if arrival_rate is not None and completion_rate is not None:
        queue_growth = arrival_rate - completion_rate

    worker_sat = None
    available = None
    headroom = None
    if active_concurrency is not None and max_concurrency is not None and max_concurrency > 0:
        worker_sat = active_concurrency / float(max_concurrency)
        available = max(0, max_concurrency - active_concurrency)
        headroom = 1.0 - worker_sat

    bottleneck = None
    state = CAP_HEALTHY
    evidence: dict[str, Any] = {
        "queue_depth": queue_depth,
        "oldest_age_seconds": oldest_age_seconds,
        "rejection_count": rejection_count,
    }

    overloaded = False
    if rejection_count is not None and rejection_count > 0 and (queue_depth or 0) > 0:
        overloaded = True
    if queue_growth is not None and queue_growth > 0 and (queue_depth or 0) > 10:
        overloaded = True
    if oldest_age_seconds is not None and oldest_age_seconds > 300:
        overloaded = True

    if overloaded:
        state = CAP_OVERLOADED
        bottleneck = BN_QUEUE
    elif worker_sat is not None and worker_sat >= 1.0:
        state = CAP_SATURATED
        bottleneck = BN_WORKER_POOL
    elif provider_saturation is not None and provider_saturation >= 0.95:
        state = CAP_SATURATED
        bottleneck = BN_PROVIDER
    elif worker_sat is not None and worker_sat >= 0.8:
        state = CAP_NEAR_CAPACITY
        bottleneck = BN_WORKER_POOL
    elif (queue_depth or 0) > 50:
        state = CAP_NEAR_CAPACITY
        bottleneck = BN_QUEUE

    return CapacityState(
        state=state,
        arrival_rate=arrival_rate,
        completion_rate=completion_rate,
        queue_growth=queue_growth,
        active_concurrency=active_concurrency,
        available_concurrency=available,
        worker_saturation=worker_sat,
        provider_saturation=provider_saturation,
        headroom=headroom,
        bottleneck_candidate=bottleneck,
        workload_class=workload_class,
        evidence=evidence,
    )


def capacity_from_runtime_snapshot(snapshot: dict[str, Any], *, workload_class: str = WORKLOAD_INTERACTIVE) -> CapacityState:
    """Adapt existing runtime.capacity_snapshot.as_dict() into CapacityState."""
    depth = snapshot.get("queue_depth_by_lane") or {}
    util = snapshot.get("utilization") or {}
    total_depth = sum(int(v) for v in depth.values())
    global_util = float(util.get("global") or 0.0)
    active = int(snapshot.get("active_workers") or 0)
    rejections = snapshot.get("rejection_counts") or {}
    reject_total = sum(int(v) for v in rejections.values())
    max_conc = active if global_util <= 0 else int(round(active / global_util)) if global_util > 0 else None
    return evaluate_capacity(
        workload_class=workload_class,
        arrival_rate=None,
        completion_rate=None,
        queue_depth=total_depth,
        oldest_age_seconds=snapshot.get("oldest_queued_age_seconds"),
        active_concurrency=active,
        max_concurrency=max_conc if max_conc and max_conc > 0 else (active + 1 if active else None),
        provider_saturation=None,
        rejection_count=reject_total,
        sample_count=MIN_SAMPLE_COUNT if (total_depth or active or reject_total) else 0,
    )
