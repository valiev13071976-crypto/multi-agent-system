"""Deterministic bottleneck analysis and scale recommendations."""

from __future__ import annotations

from typing import Any

from scale_optimization.models import (
    BN_ADMISSION,
    BN_PROVIDER,
    BN_QUEUE,
    BN_RETRY,
    BN_TENANT,
    BN_UNKNOWN,
    BN_WORKER_POOL,
    BottleneckResult,
    CapacityState,
    CAP_HEALTHY,
    CAP_INSUFFICIENT_DATA,
    CAP_NEAR_CAPACITY,
    CAP_OVERLOADED,
    CAP_SATURATED,
    REC_APPLY_BACKPRESSURE,
    REC_BATCH_DEFER,
    REC_DECREASE_POOL,
    REC_INCREASE_POOL,
    REC_INSUFFICIENT_DATA,
    REC_INVESTIGATE_CPU,
    REC_INVESTIGATE_DB,
    REC_INVESTIGATE_MEMORY,
    REC_NO_ACTION,
    REC_PROVIDER_REROUTE,
    REC_REDUCE_CONCURRENCY,
    REC_SCALE_OUT,
    REC_SCALE_UP,
    REC_SHED_LOAD,
    ScaleRecommendation,
)


def detect_bottleneck(
    *,
    capacity: CapacityState | None = None,
    queue_depth: int | None = None,
    queue_wait_p95_ms: float | None = None,
    worker_utilization: float | None = None,
    provider_429_rate: float | None = None,
    provider_latency_p95_ms: float | None = None,
    retry_ratio: float | None = None,
    tenant_share: float | None = None,
    admission_reject_rate: float | None = None,
    sample_count: int = 0,
    workload_class: str = "INTERACTIVE",
) -> BottleneckResult:
    if sample_count < 5 and capacity is None:
        return BottleneckResult(
            category=BN_UNKNOWN,
            evidence={"reason": "insufficient_data"},
            affected_workload=workload_class,
            severity="info",
            confidence="low",
            observed_metric=None,
            threshold=None,
            recommendation_class=REC_INSUFFICIENT_DATA,
        )

    if capacity and capacity.state == CAP_INSUFFICIENT_DATA:
        return BottleneckResult(
            category=BN_UNKNOWN,
            evidence=dict(capacity.evidence),
            affected_workload=workload_class,
            severity="info",
            confidence="low",
            observed_metric=None,
            threshold=None,
            recommendation_class=REC_INSUFFICIENT_DATA,
        )

    if admission_reject_rate is not None and admission_reject_rate > 0.1:
        return BottleneckResult(
            category=BN_ADMISSION,
            evidence={"admission_reject_rate": admission_reject_rate},
            affected_workload=workload_class,
            severity="crit",
            confidence="high",
            observed_metric=admission_reject_rate,
            threshold=0.1,
            recommendation_class=REC_APPLY_BACKPRESSURE,
        )

    if retry_ratio is not None and retry_ratio > 0.5:
        return BottleneckResult(
            category=BN_RETRY,
            evidence={"retry_ratio": retry_ratio},
            affected_workload=workload_class,
            severity="warn",
            confidence="high",
            observed_metric=retry_ratio,
            threshold=0.5,
            recommendation_class=REC_REDUCE_CONCURRENCY,
        )

    if tenant_share is not None and tenant_share > 0.7:
        return BottleneckResult(
            category=BN_TENANT,
            evidence={"tenant_share": tenant_share},
            affected_workload=workload_class,
            severity="warn",
            confidence="medium",
            observed_metric=tenant_share,
            threshold=0.7,
            recommendation_class=REC_APPLY_BACKPRESSURE,
        )

    if provider_429_rate is not None and provider_429_rate > 0.05:
        return BottleneckResult(
            category=BN_PROVIDER,
            evidence={"provider_429_rate": provider_429_rate},
            affected_workload=workload_class,
            severity="warn",
            confidence="high",
            observed_metric=provider_429_rate,
            threshold=0.05,
            recommendation_class=REC_PROVIDER_REROUTE,
        )

    if provider_latency_p95_ms is not None and provider_latency_p95_ms > 5000:
        return BottleneckResult(
            category=BN_PROVIDER,
            evidence={"provider_latency_p95_ms": provider_latency_p95_ms},
            affected_workload=workload_class,
            severity="warn",
            confidence="medium",
            observed_metric=provider_latency_p95_ms,
            threshold=5000,
            recommendation_class=REC_PROVIDER_REROUTE,
        )

    if (queue_depth is not None and queue_depth > 100) or (queue_wait_p95_ms is not None and queue_wait_p95_ms > 2000):
        return BottleneckResult(
            category=BN_QUEUE,
            evidence={"queue_depth": queue_depth, "queue_wait_p95_ms": queue_wait_p95_ms},
            affected_workload=workload_class,
            severity="crit" if (queue_depth or 0) > 500 else "warn",
            confidence="high",
            observed_metric=float(queue_depth or queue_wait_p95_ms or 0),
            threshold=100.0,
            recommendation_class=REC_SCALE_OUT,
        )

    if worker_utilization is not None and worker_utilization >= 0.95:
        return BottleneckResult(
            category=BN_WORKER_POOL,
            evidence={"worker_utilization": worker_utilization},
            affected_workload=workload_class,
            severity="warn",
            confidence="high",
            observed_metric=worker_utilization,
            threshold=0.95,
            recommendation_class=REC_INCREASE_POOL,
        )

    if capacity and capacity.bottleneck_candidate:
        return BottleneckResult(
            category=capacity.bottleneck_candidate,
            evidence=dict(capacity.evidence),
            affected_workload=workload_class,
            severity="warn" if capacity.state != CAP_HEALTHY else "info",
            confidence="medium",
            observed_metric=capacity.worker_saturation,
            threshold=0.8,
            recommendation_class=_rec_for_capacity(capacity.state),
        )

    return BottleneckResult(
        category=BN_UNKNOWN if sample_count < 5 else BN_QUEUE,
        evidence={"note": "no_dominant_bottleneck" if sample_count >= 5 else "insufficient_data"},
        affected_workload=workload_class,
        severity="info",
        confidence="low" if sample_count < 5 else "medium",
        observed_metric=None,
        threshold=None,
        recommendation_class=REC_INSUFFICIENT_DATA if sample_count < 5 else REC_NO_ACTION,
    )


def _rec_for_capacity(state: str) -> str:
    if state == CAP_OVERLOADED:
        return REC_SHED_LOAD
    if state == CAP_SATURATED:
        return REC_SCALE_OUT
    if state == CAP_NEAR_CAPACITY:
        return REC_INCREASE_POOL
    if state == CAP_INSUFFICIENT_DATA:
        return REC_INSUFFICIENT_DATA
    return REC_NO_ACTION


def recommend_scale(
    bottleneck: BottleneckResult,
    *,
    interactive_pressure: bool = False,
    batch_pressure: bool = False,
) -> ScaleRecommendation:
    action = bottleneck.recommendation_class
    if interactive_pressure and batch_pressure and action in {REC_SCALE_OUT, REC_INCREASE_POOL}:
        action = REC_BATCH_DEFER
    if action not in {
        REC_NO_ACTION,
        REC_SCALE_UP,
        REC_SCALE_OUT,
        REC_INCREASE_POOL,
        REC_DECREASE_POOL,
        REC_REDUCE_CONCURRENCY,
        REC_APPLY_BACKPRESSURE,
        REC_SHED_LOAD,
        REC_PROVIDER_REROUTE,
        REC_BATCH_DEFER,
        REC_INVESTIGATE_DB,
        REC_INVESTIGATE_MEMORY,
        REC_INVESTIGATE_CPU,
        REC_INSUFFICIENT_DATA,
    }:
        action = REC_INSUFFICIENT_DATA
    return ScaleRecommendation(
        action=action,
        reason=bottleneck.category,
        evidence=dict(bottleneck.evidence),
        workload_class=bottleneck.affected_workload,
    )
