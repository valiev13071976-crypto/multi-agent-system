"""Optimization feedback loop (12.7)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from seo_marketing.platform_models import (
    DECISION_CONTINUE_MEASURING,
    DECISION_INSUFFICIENT_DATA,
    DECISION_KEEP,
    DECISION_REVISE,
    DECISION_ROLLBACK_RECOMMENDED,
    MEASURE_CONFOUNDED,
    MEASURE_DECLINED,
    MEASURE_IMPROVED,
    MEASURE_NO_CLEAR_CHANGE,
    OptimizationDecision,
    OptimizationMeasurement,
    OptimizationPlan,
)
from seo_marketing.policy import MAX_REVISIONS_PER_WINDOW, MIN_MEASUREMENT_WINDOW_DAYS


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_optimization_plan(
    *,
    tenant_id: str,
    site_id: str,
    baseline_snapshot_ids: tuple[str, ...],
    actions: list[dict],
    measurement_window_days: int = MIN_MEASUREMENT_WINDOW_DAYS,
    version: int = 1,
) -> OptimizationPlan:
    return OptimizationPlan(
        plan_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        site_id=site_id,
        version=version,
        baseline_snapshot_ids=baseline_snapshot_ids,
        actions=tuple(actions),
        measurement_window_days=measurement_window_days,
        status="planned",
    )


def measure_action(
    *,
    tenant_id: str,
    plan: OptimizationPlan,
    action_id: str,
    baseline_metrics: dict,
    post_metrics: dict,
    window_start: str,
    window_end: str,
) -> OptimizationMeasurement:
    improved = False
    declined = False
    for key in baseline_metrics:
        if key not in post_metrics:
            continue
        try:
            if float(post_metrics[key]) > float(baseline_metrics[key]):
                improved = True
            elif float(post_metrics[key]) < float(baseline_metrics[key]):
                declined = True
        except (TypeError, ValueError):
            continue
    if improved and not declined:
        outcome = MEASURE_IMPROVED
    elif declined and not improved:
        outcome = MEASURE_DECLINED
    elif improved and declined:
        outcome = MEASURE_CONFOUNDED
    else:
        outcome = MEASURE_NO_CLEAR_CHANGE
    return OptimizationMeasurement(
        measurement_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        plan_id=plan.plan_id,
        action_id=action_id,
        outcome=outcome,
        metrics={"baseline": baseline_metrics, "post": post_metrics},
        window_start=window_start,
        window_end=window_end,
    )


def decide_optimization(
    *,
    tenant_id: str,
    plan: OptimizationPlan,
    action_id: str,
    measurement: OptimizationMeasurement | None,
    revisions_in_window: int = 0,
    days_since_action: int = 0,
) -> OptimizationDecision:
    if days_since_action < plan.measurement_window_days:
        return OptimizationDecision(
            decision_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            plan_id=plan.plan_id,
            action_id=action_id,
            decision=DECISION_CONTINUE_MEASURING,
            attribution="INSUFFICIENT_DATA",
        )
    if measurement is None:
        return OptimizationDecision(
            decision_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            plan_id=plan.plan_id,
            action_id=action_id,
            decision=DECISION_INSUFFICIENT_DATA,
            attribution="INSUFFICIENT_DATA",
        )
    if measurement.outcome == MEASURE_IMPROVED:
        decision = DECISION_KEEP
    elif measurement.outcome == MEASURE_DECLINED:
        decision = DECISION_ROLLBACK_RECOMMENDED if revisions_in_window < MAX_REVISIONS_PER_WINDOW else DECISION_REVISE
    else:
        decision = DECISION_REVISE
    return OptimizationDecision(
        decision_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        plan_id=plan.plan_id,
        action_id=action_id,
        decision=decision,
        attribution="OBSERVED_ASSOCIATION",
    )
