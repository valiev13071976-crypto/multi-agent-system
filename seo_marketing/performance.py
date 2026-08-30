"""Performance / speed intelligence (12.4)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from seo_marketing.platform_models import NOT_AVAILABLE, PerformanceAudit, PerformanceObservation, SeoProvenance, TRUSTED_EXTERNAL

METRIC_LCP = "LCP"
METRIC_INP = "INP"
METRIC_CLS = "CLS"
METRIC_TTFB = "TTFB"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_performance_observation(
    *,
    tenant_id: str,
    page_id: str,
    metric: str,
    value: float | None,
    unit: str,
    measurement_type: str,
    source: str,
) -> PerformanceObservation:
    trust = TRUSTED_EXTERNAL if value is not None and source != "none" else NOT_AVAILABLE
    return PerformanceObservation(
        observation_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        page_id=page_id,
        metric=metric,
        value=value,
        unit=unit,
        measurement_type=measurement_type,
        provenance=SeoProvenance(
            source=source,
            observed_at=_utc(),
            retrieved_at=_utc(),
            trust_level=trust,
        ),
    )


def audit_performance(
    *,
    tenant_id: str,
    site_id: str,
    observations: list[PerformanceObservation],
    budget: dict | None = None,
) -> PerformanceAudit:
    budget = dict(budget or {"LCP": 2.5, "INP": 200, "CLS": 0.1})
    violations: list[str] = []
    for obs in observations:
        if obs.value is None:
            continue
        limit = budget.get(obs.metric)
        if limit is not None and float(obs.value) > float(limit):
            violations.append(f"{obs.metric}_budget_exceeded")
    return PerformanceAudit(
        audit_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        site_id=site_id,
        observations=tuple(observations),
        budget_violations=tuple(violations),
        provenance=SeoProvenance(source="performance_audit", observed_at=_utc(), retrieved_at=_utc(), trust_level=TRUSTED_EXTERNAL),
    )
