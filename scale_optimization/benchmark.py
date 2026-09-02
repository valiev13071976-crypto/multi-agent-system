"""Deterministic engineering benchmark harness (fixture only — no live traffic)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from scale_optimization.config import WORKLOAD_BATCH, WORKLOAD_INTERACTIVE, WORKLOAD_NORMAL
from scale_optimization.metrics import MetricRegistry, aggregate_percentiles, build_latency_breakdown


PROFILES = (
    "A_interactive_light",
    "B_interactive_concurrent",
    "C_mixed_interactive_batch",
    "D_heavy_batch",
    "E_provider_slowdown",
    "F_provider_429",
    "G_retry_pressure",
    "H_noisy_tenant",
    "I_queue_overload",
    "J_large_excel",
    "K_crawler_batch",
    "L_persistence_pressure",
)


@dataclass
class BenchmarkResult:
    profile: str
    workload_class: str
    sample_count: int
    concurrency: int
    duration_ms: float
    throughput: float
    latency: dict[str, float]
    errors: int
    queue_metrics: dict[str, Any]
    capacity_signals: dict[str, Any]
    verdict: str
    latency_breakdown: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    mode: str = "FIXTURE"
    live: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "workload_class": self.workload_class,
            "sample_count": self.sample_count,
            "concurrency": self.concurrency,
            "duration_ms": self.duration_ms,
            "throughput": self.throughput,
            "latency": dict(self.latency),
            "errors": self.errors,
            "queue_metrics": dict(self.queue_metrics),
            "capacity_signals": dict(self.capacity_signals),
            "verdict": self.verdict,
            "latency_breakdown": dict(self.latency_breakdown),
            "cost": dict(self.cost),
            "notes": self.notes,
            "mode": self.mode,
            "live": self.live,
        }


def _sim_latencies(profile: str, n: int) -> tuple[list[float], int, dict[str, Any]]:
    """Deterministic synthetic timings — no network, no paid APIs."""
    base = {
        "A_interactive_light": (20.0, 5.0, 0),
        "B_interactive_concurrent": (45.0, 15.0, 0),
        "C_mixed_interactive_batch": (60.0, 25.0, 0),
        "D_heavy_batch": (200.0, 40.0, 0),
        "E_provider_slowdown": (800.0, 100.0, 0),
        "F_provider_429": (100.0, 20.0, max(1, n // 10)),
        "G_retry_pressure": (150.0, 30.0, max(1, n // 5)),
        "H_noisy_tenant": (70.0, 20.0, 0),
        "I_queue_overload": (300.0, 80.0, 0),
        "J_large_excel": (500.0, 50.0, 0),
        "K_crawler_batch": (400.0, 60.0, 0),
        "L_persistence_pressure": (120.0, 40.0, 0),
    }.get(profile, (50.0, 10.0, 0))
    mean, spread, errors = base
    values = [mean + (i % 7) * (spread / 7.0) for i in range(n)]
    extras = {
        "provider_429": profile == "F_provider_429",
        "retry_amplification": profile == "G_retry_pressure",
        "noisy_tenant_share": 0.8 if profile == "H_noisy_tenant" else 0.2,
        "queue_depth": 250 if profile == "I_queue_overload" else (5 if "interactive" in profile else 20),
        "input_rows": 10000 if profile == "J_large_excel" else (0 if "interactive" in profile else 100),
        "pages": 500 if profile == "K_crawler_batch" else 0,
        "persist_ms": 80.0 if profile == "L_persistence_pressure" else 5.0,
    }
    return values, errors, extras


def run_profile(
    profile: str,
    *,
    sample_count: int = 20,
    concurrency: int = 4,
    metrics: MetricRegistry | None = None,
) -> BenchmarkResult:
    if profile not in PROFILES:
        raise ValueError(f"unknown_profile:{profile}")
    reg = metrics or MetricRegistry()
    t0 = time.perf_counter()
    values, errors, extras = _sim_latencies(profile, sample_count)
    for i, v in enumerate(values):
        wl = WORKLOAD_INTERACTIVE if profile.startswith(("A_", "B_")) else (
            WORKLOAD_BATCH if profile.startswith(("D_", "J_", "K_")) else WORKLOAD_NORMAL
        )
        reg.emit("request_latency_ms", v, labels={"workload_class": wl, "operation": profile[:32]})
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    stats = aggregate_percentiles(values)
    queue_depth = int(extras["queue_depth"])
    breakdown = build_latency_breakdown(
        queue_wait_ms=min(stats.p50, extras.get("persist_ms", 5) * 2),
        provider_time_ms=stats.p50 * (0.7 if extras.get("provider_429") or profile == "E_provider_slowdown" else 0.3),
        persistence_time_ms=float(extras["persist_ms"]),
        workflow_time_ms=stats.p50 * 0.2,
        response_finalize_time_ms=2.0,
    )
    # Verdict based on synthetic SLO for interactive profiles
    verdict = "PASS"
    if profile.startswith(("A_", "B_")) and stats.p95 > 2000:
        verdict = "FAIL"
    if errors > sample_count * 0.5:
        verdict = "FAIL"
    if profile == "I_queue_overload":
        verdict = "DEGRADED"

    throughput = sample_count / max(elapsed_ms / 1000.0, 0.001)
    original = sample_count
    retries = int(errors * 2) if extras.get("retry_amplification") else 0
    cost_units = (original + retries) * (0.002 if "provider" in profile or profile.startswith("E_") else 0.0005)

    return BenchmarkResult(
        profile=profile,
        workload_class=WORKLOAD_INTERACTIVE if profile.startswith(("A_", "B_")) else (
            WORKLOAD_BATCH if profile.startswith(("D_", "J_", "K_")) else WORKLOAD_NORMAL
        ),
        sample_count=sample_count,
        concurrency=concurrency,
        duration_ms=round(elapsed_ms, 4),
        throughput=round(throughput, 4),
        latency=stats.as_dict(),
        errors=errors,
        queue_metrics={"depth": queue_depth, "oldest_age_seconds": queue_depth * 0.5},
        capacity_signals={
            "worker_utilization": min(0.99, 0.2 + queue_depth / 500.0),
            "provider_saturation": 0.95 if extras.get("provider_429") else 0.3,
            "retry_ratio": (retries / max(original, 1)) if retries else 0.0,
            "noisy_tenant_share": extras["noisy_tenant_share"],
            "input_rows": extras["input_rows"],
            "pages": extras["pages"],
        },
        verdict=verdict,
        latency_breakdown=breakdown.as_dict(),
        cost={
            "cost_units": round(cost_units, 6),
            "original_requests": original,
            "retry_attempts": retries,
            "retry_added_load": retries,
        },
        notes="deterministic_fixture_benchmark",
    )


def run_all_profiles(**kwargs) -> list[BenchmarkResult]:
    return [run_profile(p, **kwargs) for p in PROFILES]
