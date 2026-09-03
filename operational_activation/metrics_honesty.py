"""Metrics & load honesty layer — no fabricated production observations."""

from __future__ import annotations

from typing import Any

from operational_activation.status import (
    ENGINEERING_READY,
    HUMAN_APPROVAL_REQUIRED,
    METRICS_INSTRUMENTATION_READY,
    REAL_PRODUCTION_SAMPLE_INSUFFICIENT,
    WAITING_FOR_EVIDENCE,
)
from scale_optimization.metrics import aggregate_percentiles


REQUIRED_METRIC_NAMES = (
    "requests",
    "success_error",
    "latency",
    "queue_wait",
    "workflow_duration",
    "tool_duration",
    "provider_duration",
    "token_usage",
    "cost_attribution",
    "tenant_attribution",
    "retry",
    "timeout",
    "circuit_breaker",
    "rate_limit",
    "admission_rejection",
    "worker_utilization",
    "queue_depth",
    "automation_execution",
    "hitl_wait",
    "external_integration_errors",
)


def metrics_instrumentation_status(*, real_sample_count: int = 0) -> dict[str, Any]:
    if real_sample_count <= 0:
        return {
            "status": METRICS_INSTRUMENTATION_READY,
            "detail": REAL_PRODUCTION_SAMPLE_INSUFFICIENT,
            "real_sample": False,
            "real_sample_count": 0,
            "instrumentation": list(REQUIRED_METRIC_NAMES),
            "closed_from_fixture_traffic": False,
        }
    return {
        "status": "METRICS_COLLECTING",
        "real_sample": True,
        "real_sample_count": real_sample_count,
        "instrumentation": list(REQUIRED_METRIC_NAMES),
    }


def percentile_dashboard_capability(*, values: list[float] | None = None) -> dict[str, Any]:
    """Validate p50/p95/p99 math with deterministic fixtures; do not claim production percentiles."""
    fixture = values if values is not None else [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    stats = aggregate_percentiles(fixture)
    return {
        "status": ENGINEERING_READY,
        "real_sample": False,
        "dimensions": [
            "endpoint",
            "workflow",
            "tool",
            "provider",
            "model",
            "tenant",
            "interactive_vs_batch",
            "time_window",
        ],
        "also_supported": [
            "queue_wait",
            "provider_latency",
            "error_rate",
            "timeout_rate",
            "retry_rate",
            "cost_per_request",
            "tokens_per_request",
            "cost_per_tenant",
            "budget_utilization",
        ],
        "fixture_math": {
            "count": stats.count,
            "p50": stats.p50,
            "p95": stats.p95,
            "p99": stats.p99,
            "avg": stats.avg,
            "max": stats.max,
        },
        "production_claim": "NOT_CLAIMED",
    }


def load_harness_safety() -> dict[str, Any]:
    return {
        "status": ENGINEERING_READY,
        "distinguishes": {
            "synthetic_offline": True,
            "staging": True,
            "production_controlled": True,
            "organic_user": True,
        },
        "production_load_executed": False,
        "live_boundary": HUMAN_APPROVAL_REQUIRED,
        "existing_harness": "production_validation.load_harness.LoadHarness.run_live → BLOCKED pending operator",
        "approval_template": {
            "target": "",
            "rps_concurrency": "",
            "duration": "",
            "endpoints": [],
            "expected_cost": "",
            "provider_call_policy": "deny_or_fixture_only",
            "rollback_abort_threshold": "",
            "monitoring": "",
            "risk": "",
        },
    }


def bottleneck_status(*, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    if not evidence:
        return {
            "status": WAITING_FOR_EVIDENCE,
            "bottleneck": "NONE",
            "detail": "NO PROVEN PRODUCTION BOTTLENECK YET.",
            "confidence": "n/a",
        }
    return {
        "status": ENGINEERING_READY,
        "bottleneck": evidence.get("name"),
        "evidence": evidence,
        "affected_path": evidence.get("affected_path"),
        "user_impact": evidence.get("user_impact"),
        "confidence": evidence.get("confidence"),
        "alternative_explanations": evidence.get("alternative_explanations", []),
        "minimum_remediation": evidence.get("minimum_remediation"),
    }


def optimization_status(*, bottleneck_proven: bool = False) -> dict[str, Any]:
    if not bottleneck_proven:
        return {
            "status": WAITING_FOR_EVIDENCE,
            "optimization_executed": False,
            "rule": "NO OPTIMIZATION WITHOUT BLOCK 30 EVIDENCE",
        }
    return {
        "status": HUMAN_APPROVAL_REQUIRED,
        "optimization_executed": False,
        "note": "Proven bottleneck requires smallest remediation + before/after benchmarks",
    }
