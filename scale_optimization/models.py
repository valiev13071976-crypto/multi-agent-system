"""Domain models for scale optimization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# SLO statuses
SLO_HEALTHY = "HEALTHY"
SLO_WARNING = "WARNING"
SLO_BREACHED = "BREACHED"
SLO_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# Capacity states
CAP_HEALTHY = "HEALTHY"
CAP_NEAR_CAPACITY = "NEAR_CAPACITY"
CAP_SATURATED = "SATURATED"
CAP_OVERLOADED = "OVERLOADED"
CAP_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# Bottleneck categories
BN_ADMISSION = "ADMISSION"
BN_QUEUE = "QUEUE"
BN_WORKER_POOL = "WORKER_POOL"
BN_CPU = "CPU"
BN_MEMORY = "MEMORY"
BN_DATABASE = "DATABASE"
BN_PROVIDER = "PROVIDER"
BN_TOOL = "TOOL"
BN_EXTERNAL = "EXTERNAL_INTEGRATION"
BN_WORKFLOW = "WORKFLOW"
BN_RETRY = "RETRY_AMPLIFICATION"
BN_TENANT = "TENANT_HOTSPOT"
BN_UNKNOWN = "UNKNOWN"

# Scale recommendations
REC_NO_ACTION = "NO_ACTION"
REC_SCALE_UP = "SCALE_UP"
REC_SCALE_OUT = "SCALE_OUT"
REC_INCREASE_POOL = "INCREASE_POOL"
REC_DECREASE_POOL = "DECREASE_POOL"
REC_REDUCE_CONCURRENCY = "REDUCE_CONCURRENCY"
REC_APPLY_BACKPRESSURE = "APPLY_BACKPRESSURE"
REC_SHED_LOAD = "SHED_LOAD"
REC_PROVIDER_REROUTE = "PROVIDER_REROUTE"
REC_BATCH_DEFER = "BATCH_DEFER"
REC_INVESTIGATE_DB = "INVESTIGATE_DB"
REC_INVESTIGATE_MEMORY = "INVESTIGATE_MEMORY"
REC_INVESTIGATE_CPU = "INVESTIGATE_CPU"
REC_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

RECOMMENDATIONS = frozenset({
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
})


@dataclass
class MetricPoint:
    name: str
    value: float
    timestamp: str
    labels: dict[str, str] = field(default_factory=dict)
    unit: str = ""


@dataclass
class LatencyBreakdown:
    total_ms: float = 0.0
    admission_wait_ms: float = 0.0
    queue_wait_ms: float = 0.0
    worker_wait_ms: float = 0.0
    workflow_time_ms: float = 0.0
    tool_time_ms: float = 0.0
    provider_time_ms: float = 0.0
    persistence_time_ms: float = 0.0
    response_finalize_time_ms: float = 0.0
    not_applicable: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_ms": self.total_ms,
            "admission_wait_ms": self.admission_wait_ms,
            "queue_wait_ms": self.queue_wait_ms,
            "worker_wait_ms": self.worker_wait_ms,
            "workflow_time_ms": self.workflow_time_ms,
            "tool_time_ms": self.tool_time_ms,
            "provider_time_ms": self.provider_time_ms,
            "persistence_time_ms": self.persistence_time_ms,
            "response_finalize_time_ms": self.response_finalize_time_ms,
            "not_applicable": list(self.not_applicable),
        }


@dataclass
class PercentileStats:
    count: int
    p50: float
    p95: float
    p99: float
    avg: float
    max: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "avg": self.avg,
            "max": self.max,
        }


@dataclass
class SLOEvaluation:
    metric: str
    target: float
    observed: float | None
    window_seconds: float
    workload_class: str
    sample_count: int
    min_samples: int
    status: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "target": self.target,
            "observed": self.observed,
            "window_seconds": self.window_seconds,
            "workload_class": self.workload_class,
            "sample_count": self.sample_count,
            "min_samples": self.min_samples,
            "status": self.status,
            "details": dict(self.details),
        }


@dataclass
class CapacityState:
    state: str
    arrival_rate: float | None
    completion_rate: float | None
    queue_growth: float | None
    active_concurrency: int | None
    available_concurrency: int | None
    worker_saturation: float | None
    provider_saturation: float | None
    headroom: float | None
    bottleneck_candidate: str | None
    workload_class: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "arrival_rate": self.arrival_rate,
            "completion_rate": self.completion_rate,
            "queue_growth": self.queue_growth,
            "active_concurrency": self.active_concurrency,
            "available_concurrency": self.available_concurrency,
            "worker_saturation": self.worker_saturation,
            "provider_saturation": self.provider_saturation,
            "headroom": self.headroom,
            "bottleneck_candidate": self.bottleneck_candidate,
            "workload_class": self.workload_class,
            "evidence": dict(self.evidence),
        }


@dataclass
class BottleneckResult:
    category: str
    evidence: dict[str, Any]
    affected_workload: str
    severity: str
    confidence: str
    observed_metric: float | None
    threshold: float | None
    recommendation_class: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "evidence": dict(self.evidence),
            "affected_workload": self.affected_workload,
            "severity": self.severity,
            "confidence": self.confidence,
            "observed_metric": self.observed_metric,
            "threshold": self.threshold,
            "recommendation_class": self.recommendation_class,
        }


@dataclass
class ScaleRecommendation:
    action: str
    reason: str
    evidence: dict[str, Any]
    workload_class: str
    cooldown_ok: bool = True
    hysteresis_ok: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "workload_class": self.workload_class,
            "cooldown_ok": self.cooldown_ok,
            "hysteresis_ok": self.hysteresis_ok,
            "infrastructure_mutation": False,
        }


@dataclass
class OptimizationEvidence:
    path: str
    before: dict[str, Any]
    change: str
    after: dict[str, Any]
    workload_profile: str
    improvement: bool
    correctness_ok: bool
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before": dict(self.before),
            "change": self.change,
            "after": dict(self.after),
            "workload_profile": self.workload_profile,
            "improvement": self.improvement,
            "correctness_ok": self.correctness_ok,
            "notes": self.notes,
        }
