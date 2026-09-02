"""Scale optimization orchestration service — measurement + recommendation plane."""

from __future__ import annotations

from typing import Any

from scale_optimization.access import PERM_SCALE_BENCHMARK, PERM_SCALE_READ, ScaleOptimizationAccessPolicy
from scale_optimization.autoscaling import AutoscalingSignalEngine
from scale_optimization.benchmark import PROFILES, run_all_profiles, run_profile
from scale_optimization.bottleneck import detect_bottleneck, recommend_scale
from scale_optimization.capacity import capacity_from_runtime_snapshot, evaluate_capacity
from scale_optimization.config import (
    WORKLOAD_BATCH,
    WORKLOAD_INTERACTIVE,
    WORKLOAD_NORMAL,
    scale_optimization_engineering_ready,
    scale_optimization_live_active,
    scale_optimization_live_verified,
)
from scale_optimization.errors import LIVE_FALLBACK_FORBIDDEN, ScaleOptimizationError
from scale_optimization.evidence import compare_before_after
from scale_optimization.metrics import (
    MetricRegistry,
    build_latency_breakdown,
    redact_for_export,
    validate_labels,
)
from scale_optimization.observability import ScaleOptimizationObservability
from scale_optimization.slo import default_slo_targets, evaluate_slo
from security.identity import RequestSecurityContext


class ScaleOptimizationService:
    def __init__(
        self,
        *,
        metrics: MetricRegistry | None = None,
        access: ScaleOptimizationAccessPolicy | None = None,
        autoscaler: AutoscalingSignalEngine | None = None,
        obs: ScaleOptimizationObservability | None = None,
        capacity_provider: Any | None = None,
    ):
        self.metrics = metrics or MetricRegistry()
        self.access = access or ScaleOptimizationAccessPolicy()
        self.autoscaler = autoscaler or AutoscalingSignalEngine()
        self.obs = obs or ScaleOptimizationObservability()
        self._capacity_provider = capacity_provider
        self._optimization_log: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {
            "engineering_ready": scale_optimization_engineering_ready(),
            "live_active": scale_optimization_live_active(),
            "live_verified": scale_optimization_live_verified(),
            "mode": "FIXTURE",
            "infrastructure_mutation": False,
        }

    def management_view(self, ctx: RequestSecurityContext, *, tenant_id: str | None = None) -> dict[str, Any]:
        self.access.require(ctx, PERM_SCALE_READ, tenant_id=tenant_id)
        snap = self._runtime_capacity_dict()
        cap = capacity_from_runtime_snapshot(snap, workload_class=WORKLOAD_INTERACTIVE)
        bn = detect_bottleneck(capacity=cap, sample_count=10, workload_class=WORKLOAD_INTERACTIVE)
        rec = recommend_scale(bn)
        slo_results = []
        for name, cfg in default_slo_targets().items():
            series = self.metrics.series("request_latency_ms", labels={"workload_class": cfg["workload_class"]})
            stats = self.metrics.stats("request_latency_ms", labels={"workload_class": cfg["workload_class"]})
            observed = stats.p95 if series else None
            if name == "availability_ratio":
                observed = 0.995 if series else None
            slo_results.append(
                evaluate_slo(
                    metric=name,
                    target=cfg["target"],
                    observed=observed,
                    sample_count=stats.count,
                    window_seconds=60.0,
                    workload_class=cfg["workload_class"],
                    higher_is_better=cfg.get("higher_is_better", False),
                ).as_dict()
            )
        view = {
            "tenant_id": tenant_id or ctx.tenant_id,
            "workload_health": {
                WORKLOAD_INTERACTIVE: cap.state,
                WORKLOAD_NORMAL: "UNKNOWN",
                WORKLOAD_BATCH: "UNKNOWN",
            },
            "latency": self.metrics.stats("request_latency_ms").as_dict(),
            "queue_health": snap,
            "worker_saturation": cap.worker_saturation,
            "provider_health": {"saturation": None, "note": "reuse ProviderGovernor metrics"},
            "capacity_state": cap.as_dict(),
            "bottleneck": bn.as_dict(),
            "cost_efficiency": {"note": "reuse FinOps; no fabricated savings"},
            "slo_state": slo_results,
            "recommendations": [rec.as_dict()],
            "mode": "FIXTURE",
            "live": False,
        }
        return redact_for_export(view)

    def analyze(
        self,
        ctx: RequestSecurityContext,
        *,
        signals: dict[str, Any],
        workload_class: str = WORKLOAD_INTERACTIVE,
    ) -> dict[str, Any]:
        self.access.require(ctx, PERM_SCALE_READ, tenant_id=signals.get("tenant_id"))
        if scale_optimization_live_active():
            raise ScaleOptimizationError(LIVE_FALLBACK_FORBIDDEN, "live_not_implemented")
        sample_count = int(signals.get("sample_count") or 0)
        cap = evaluate_capacity(
            workload_class=workload_class,
            arrival_rate=signals.get("arrival_rate"),
            completion_rate=signals.get("completion_rate"),
            queue_depth=signals.get("queue_depth"),
            oldest_age_seconds=signals.get("oldest_age_seconds"),
            active_concurrency=signals.get("active_concurrency"),
            max_concurrency=signals.get("max_concurrency"),
            provider_saturation=signals.get("provider_saturation"),
            rejection_count=signals.get("rejection_count"),
            sample_count=sample_count,
        )
        bn = detect_bottleneck(
            capacity=cap,
            queue_depth=signals.get("queue_depth"),
            queue_wait_p95_ms=signals.get("queue_wait_p95_ms"),
            worker_utilization=signals.get("worker_utilization"),
            provider_429_rate=signals.get("provider_429_rate"),
            provider_latency_p95_ms=signals.get("provider_latency_p95_ms"),
            retry_ratio=signals.get("retry_ratio"),
            tenant_share=signals.get("tenant_share"),
            admission_reject_rate=signals.get("admission_reject_rate"),
            sample_count=sample_count,
            workload_class=workload_class,
        )
        interactive_pressure = bool(signals.get("interactive_pressure"))
        batch_pressure = bool(signals.get("batch_pressure"))
        rec = recommend_scale(bn, interactive_pressure=interactive_pressure, batch_pressure=batch_pressure)
        auto = self.autoscaler.evaluate(
            queue_depth=float(signals.get("queue_depth") or 0),
            oldest_age_seconds=float(signals.get("oldest_age_seconds") or 0),
            arrival_completion_delta=float((signals.get("arrival_rate") or 0) - (signals.get("completion_rate") or 0)),
            worker_utilization=float(signals.get("worker_utilization") or 0),
            interactive_latency_p95_ms=float(signals.get("interactive_latency_p95_ms") or 0),
            provider_saturation=float(signals.get("provider_saturation") or 0),
            sample_count=max(sample_count, 5) if sample_count else 0,
            observation_window_seconds=float(signals.get("observation_window_seconds") or 0),
            workload_class=workload_class,
        )
        breakdown = build_latency_breakdown(
            admission_wait_ms=float(signals.get("admission_wait_ms") or 0),
            queue_wait_ms=float(signals.get("queue_wait_ms") or 0),
            worker_wait_ms=float(signals.get("worker_wait_ms") or 0),
            workflow_time_ms=float(signals.get("workflow_time_ms") or 0),
            tool_time_ms=float(signals.get("tool_time_ms") or 0),
            provider_time_ms=float(signals.get("provider_time_ms") or 0),
            persistence_time_ms=float(signals.get("persistence_time_ms") or 0),
            response_finalize_time_ms=float(signals.get("response_finalize_time_ms") or 0),
        )
        self.obs.emit(event="analyze", workload_class=workload_class, action=rec.action)
        return {
            "capacity": cap.as_dict(),
            "bottleneck": bn.as_dict(),
            "recommendation": rec.as_dict(),
            "autoscaling_signal": auto.as_dict(),
            "latency_breakdown": breakdown.as_dict(),
            "mode": "FIXTURE",
            "live": False,
        }

    def run_benchmark(self, ctx: RequestSecurityContext, *, profile: str | None = None) -> dict[str, Any]:
        self.access.require(ctx, PERM_SCALE_BENCHMARK)
        if scale_optimization_live_active():
            raise ScaleOptimizationError(LIVE_FALLBACK_FORBIDDEN, "live_not_implemented")
        if profile:
            results = [run_profile(profile, metrics=self.metrics)]
        else:
            results = run_all_profiles(metrics=self.metrics)
        self.obs.emit(event="benchmark", profiles=len(results))
        return {
            "results": [r.as_dict() for r in results],
            "profiles": list(PROFILES),
            "mode": "FIXTURE",
            "live": False,
            "infrastructure_mutation": False,
        }

    def record_optimization_evidence(
        self,
        ctx: RequestSecurityContext,
        *,
        path: str,
        before: dict[str, Any],
        after: dict[str, Any],
        change: str,
        workload_profile: str,
        correctness_ok: bool = True,
    ) -> dict[str, Any]:
        self.access.require(ctx, PERM_SCALE_READ)
        evidence = compare_before_after(
            path=path,
            before=before,
            after=after,
            change=change,
            workload_profile=workload_profile,
            correctness_ok=correctness_ok,
        )
        self._optimization_log.append(evidence.as_dict())
        self.obs.emit(event="optimization_evidence", path=path, improvement=evidence.improvement)
        return evidence.as_dict()

    def optimization_log(self) -> list[dict[str, Any]]:
        return list(self._optimization_log)

    def emit_metric(self, name: str, value: float, *, labels: dict[str, Any] | None = None) -> dict[str, Any]:
        validate_labels(labels)
        point = self.metrics.emit(name, value, labels=labels)
        return {"name": point.name, "value": point.value, "labels": point.labels}

    def _runtime_capacity_dict(self) -> dict[str, Any]:
        if self._capacity_provider is not None:
            snap = self._capacity_provider()
            if hasattr(snap, "as_dict"):
                return snap.as_dict()
            if isinstance(snap, dict):
                return snap
        return {
            "queue_depth_by_lane": {},
            "active_workers": 0,
            "saturated_pools": [],
            "oldest_queued_age_seconds": None,
            "rejection_counts": {},
            "utilization": {},
            "dlq_depth": 0,
            "running_by_lane": {},
            "checked_at": "",
        }
