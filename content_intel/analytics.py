"""Deterministic performance analytics — no LLM arithmetic."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from content_intel.errors import CONTENT_METRICS_INVALID, ContentIntelError
from content_intel.platform_models import (
    ANALYTICS_PROFILE_VERSION,
    PerformanceObservation,
    PerformanceReport,
)


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ContentIntelError(CONTENT_METRICS_INVALID) from None


def compute_ctr(clicks: Decimal | None, impressions: Decimal | None) -> dict:
    if clicks is None or impressions is None:
        return {"ctr": None, "status": "missing"}
    if impressions == 0:
        return {"ctr": None, "status": "zero_denominator"}
    return {"ctr": (clicks / impressions).quantize(Decimal("0.0001")), "status": "ok"}


def compute_engagement_rate(engagements: Decimal | None, reach: Decimal | None) -> dict:
    if engagements is None or reach is None:
        return {"engagement_rate": None, "status": "missing"}
    if reach == 0:
        return {"engagement_rate": None, "status": "zero_denominator"}
    return {
        "engagement_rate": (engagements / reach).quantize(Decimal("0.0001")),
        "status": "ok",
    }


def compute_completion_rate(completions: Decimal | None, views: Decimal | None) -> dict:
    if completions is None or views is None:
        return {"completion_rate": None, "status": "missing"}
    if views == 0:
        return {"completion_rate": None, "status": "zero_denominator"}
    return {
        "completion_rate": (completions / views).quantize(Decimal("0.0001")),
        "status": "ok",
    }


class PerformanceAnalytics:
    profile_version = ANALYTICS_PROFILE_VERSION

    def ingest_observations(
        self,
        rows: list[dict],
        *,
        tenant_id: str,
    ) -> tuple[PerformanceObservation, ...]:
        out: list[PerformanceObservation] = []
        for row in rows:
            val = row.get("metric_value")
            if val is not None and float(val) < 0 and row.get("metric_name") == "impressions":
                raise ContentIntelError(CONTENT_METRICS_INVALID)
            obs = PerformanceObservation(
                observation_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                asset_version_id=str(row["asset_version_id"]),
                channel=str(row.get("channel") or "unknown"),
                metric_name=str(row["metric_name"]),
                metric_value=_dec(val),
                unit=str(row.get("unit") or "count"),
                window_start=row.get("window_start") or datetime.now(timezone.utc),
                window_end=row.get("window_end") or datetime.now(timezone.utc),
                source=str(row.get("source") or "supplied"),
                status="present" if val is not None else "missing",
            )
            out.append(obs)
        return tuple(out)

    def analyze(
        self,
        observations: tuple[PerformanceObservation, ...],
        *,
        tenant_id: str,
        project_id: str,
    ) -> PerformanceReport:
        by_metric: dict[str, list[PerformanceObservation]] = {}
        for obs in observations:
            if obs.tenant_id != tenant_id:
                raise ContentIntelError(CONTENT_METRICS_INVALID)
            by_metric.setdefault(obs.metric_name, []).append(obs)

        def _sum(name: str) -> Decimal | None:
            rows = by_metric.get(name, [])
            vals = [o.metric_value for o in rows if o.metric_value is not None]
            if not vals:
                return None
            return sum(vals, Decimal("0"))

        metrics = {
            "ctr": compute_ctr(_sum("clicks"), _sum("impressions")),
            "engagement_rate": compute_engagement_rate(_sum("engagements"), _sum("reach")),
            "completion_rate": compute_completion_rate(_sum("completions"), _sum("views")),
            "impressions_total": str(_sum("impressions")) if _sum("impressions") is not None else None,
        }
        limitations: list[str] = []
        channels = {o.channel for o in observations}
        if len(channels) > 1:
            limitations.append("cross_channel_metrics_not_directly_comparable")
        return PerformanceReport(
            report_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            project_id=project_id,
            observations=observations,
            metrics_computed=metrics,
            limitations=tuple(limitations),
        )
