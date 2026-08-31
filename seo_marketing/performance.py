"""Performance / speed intelligence (12.4)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from seo_marketing.errors import SeoMarketingError
from seo_marketing.platform_models import (
    CWV_BUDGET_VERSION,
    CWVBudget,
    NOT_AVAILABLE,
    PerformanceAudit,
    PerformanceObservation,
    SeoProvenance,
    TRUSTED_EXTERNAL,
)

METRIC_LCP = "LCP"
METRIC_INP = "INP"
METRIC_CLS = "CLS"
METRIC_TTFB = "TTFB"
METRIC_FCP = "FCP"

# Versioned thresholds — do not scatter mutable budgets across adapters
CWV_BUDGET_LAB = CWVBudget(
    budget_id="cwv_lab_v1",
    version=CWV_BUDGET_VERSION,
    measurement_type="LAB",
    thresholds={"LCP": 2.5, "INP": 200, "CLS": 0.1, "TTFB": 0.8, "FCP": 1.8},
)
CWV_BUDGET_FIELD = CWVBudget(
    budget_id="cwv_field_v1",
    version=CWV_BUDGET_VERSION,
    measurement_type="FIELD",
    thresholds={"LCP": 2.5, "INP": 200, "CLS": 0.1},
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_cwv_budget(measurement_type: str = "LAB") -> CWVBudget:
    mt = measurement_type.upper()
    if mt == "FIELD":
        return CWV_BUDGET_FIELD
    if mt == "LAB":
        return CWV_BUDGET_LAB
    raise SeoMarketingError("SEO_VALIDATION_FAILED", f"unknown_measurement_type:{measurement_type}")


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
        measurement_type=measurement_type.upper(),
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
    measurement_type: str | None = None,
) -> PerformanceAudit:
    # Do not mix FIELD and LAB in one audit
    types = {o.measurement_type.upper() for o in observations if o.measurement_type}
    if len(types) > 1:
        raise SeoMarketingError("SEO_CONFLICT", "field_lab_mixed")
    mt = measurement_type or (next(iter(types)) if types else "LAB")
    cwv = get_cwv_budget(mt)
    limits = dict(budget) if budget is not None else dict(cwv.thresholds)
    violations: list[str] = []
    for obs in observations:
        if obs.value is None:
            continue
        if obs.measurement_type.upper() != mt.upper():
            raise SeoMarketingError("SEO_CONFLICT", "field_lab_mixed")
        limit = limits.get(obs.metric)
        if limit is not None and float(obs.value) > float(limit):
            violations.append(f"{obs.metric}_budget_exceeded")
    return PerformanceAudit(
        audit_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        site_id=site_id,
        observations=tuple(observations),
        budget_violations=tuple(violations),
        provenance=SeoProvenance(
            source="performance_audit",
            observed_at=_utc(),
            retrieved_at=_utc(),
            trust_level=TRUSTED_EXTERNAL,
            source_version=cwv.version,
        ),
    )


def performance_recommendations(observations: list[PerformanceObservation]) -> list[dict]:
    """Evidence-based categories only — no invented measurements."""
    recs: list[dict] = []
    for obs in observations:
        if obs.value is None:
            continue
        if obs.metric == METRIC_LCP and float(obs.value) > 2.5:
            recs.append({"type": "PERFORMANCE", "category": "image_optimization", "evidence": obs.observation_id})
        if obs.metric == METRIC_CLS and float(obs.value) > 0.1:
            recs.append({"type": "PERFORMANCE", "category": "layout_instability", "evidence": obs.observation_id})
        if obs.metric == METRIC_INP and float(obs.value) > 200:
            recs.append({"type": "PERFORMANCE", "category": "js_weight", "evidence": obs.observation_id})
        if obs.metric == METRIC_TTFB and float(obs.value) > 0.8:
            recs.append({"type": "PERFORMANCE", "category": "server_response", "evidence": obs.observation_id})
    return recs
