"""Expansion and scale gates — evidence-based; no Railway mutation."""

from __future__ import annotations

from typing import Any

from operational_activation.status import (
    ENGINEERING_READY,
    HUMAN_APPROVAL_REQUIRED,
    WAITING_FOR_EVIDENCE,
)


def expansion_gates(
    *,
    unresolved_p0: int = 0,
    unresolved_p1: int = 0,
    auth_stable: bool = True,
    tenant_isolation_stable: bool = True,
    budget_visible: bool = True,
    error_rate_visible: bool = True,
    support_ready: bool = True,
    legal_onboarding_ready: bool = False,
    rollback_available: bool = True,
    pilot_criteria_passed: bool | None = None,
) -> dict[str, Any]:
    stages = ("cohort_25", "cohort_100", "cohort_500", "public_open")
    blocking = []
    if unresolved_p0 > 0:
        blocking.append("unresolved_p0")
    if pilot_criteria_passed is not True:
        blocking.append("pilot_criteria_not_passed")
    if not legal_onboarding_ready:
        blocking.append("legal_onboarding_pending")
    return {
        "status": ENGINEERING_READY if not blocking else WAITING_FOR_EVIDENCE,
        "expanded_users": False,
        "real_expand_boundary": HUMAN_APPROVAL_REQUIRED,
        "configurable_stages": list(stages),
        "gates": {
            "unresolved_p0": unresolved_p0,
            "unresolved_p1": unresolved_p1,
            "auth_stable": auth_stable,
            "tenant_isolation_stable": tenant_isolation_stable,
            "budget_cost_visible": budget_visible,
            "error_rate_visible": error_rate_visible,
            "support_process_ready": support_ready,
            "legal_product_onboarding_ready": legal_onboarding_ready,
            "rollback_available": rollback_available,
            "pilot_criteria_passed": pilot_criteria_passed,
        },
        "blocking": blocking,
    }


def scale_decision(*, signals: dict[str, Any] | None = None) -> dict[str, Any]:
    """No speculative scale. Without production signals → NO SCALE ACTION REQUIRED."""
    signals = signals or {}
    evidence_keys = (
        "cpu_saturation",
        "memory",
        "queue_depth",
        "queue_wait_p95_ms",
        "worker_utilization",
        "db_load",
        "p95_ms",
        "p99_ms",
        "provider_constraints",
    )
    present = {k: signals[k] for k in evidence_keys if k in signals and signals[k] is not None}
    if not present:
        return {
            "status": "NO_SCALE_ACTION_REQUIRED",
            "infra_mutation": False,
            "railway_mutation": False,
            "reason": "no_production_capacity_signals",
            "approval_boundary": HUMAN_APPROVAL_REQUIRED,
            "evidence": {},
        }
    # Even with signals, mutation requires human approval
    return {
        "status": HUMAN_APPROVAL_REQUIRED,
        "infra_mutation": False,
        "railway_mutation": False,
        "reason": "signals_present_but_mutation_requires_approval",
        "evidence": present,
        "possible_actions": [
            "vertical_scale",
            "horizontal_worker_scale",
            "queue_worker_adjustment",
            "db_optimization",
            "cache",
            "provider_distribution",
        ],
    }
