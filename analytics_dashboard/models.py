"""Analytics dashboard models."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

STATUS_OK = "OK"
STATUS_NO_DATA = "NO_DATA"
STATUS_PARTIAL = "PARTIAL"
STATUS_STALE = "STALE"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_NOT_SUPPORTED = "NOT_SUPPORTED"


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    display_name: str
    domain: str
    source: str
    unit: str
    aggregation: str
    dimensions: tuple[str, ...] = ()
    description: str = ""


@dataclass
class AnalyticsQuery:
    tenant_id: str
    metrics: list[str]
    start: str
    end: str
    timezone: str = "Europe/Moscow"
    granularity: str = "day"
    filters: dict[str, Any] = field(default_factory=dict)
    group_by: list[str] = field(default_factory=list)
    limit: int = 50
    offset: int = 0


@dataclass
class MetricValue:
    metric_id: str
    value: str | int | None
    unit: str = ""
    currency: str = ""
    status: str = STATUS_OK
    dimensions: dict[str, str] = field(default_factory=dict)
    source: str = ""
    freshness_at: str = ""
    generated_at: str = ""
    partial: bool = False
    warnings: tuple[str, ...] = ()


@dataclass
class TimeSeriesPoint:
    bucket_start: str
    value: str
    status: str = STATUS_OK


@dataclass
class AlertSignal:
    alert_id: str
    alert_type: str
    severity: str
    tenant_id: str
    domain: str
    message: str
    evidence: dict[str, Any]
    timestamp: str
    status: str = "OPEN"
