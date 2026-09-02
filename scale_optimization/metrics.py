"""Metric contract: aggregation, percentiles, label safety."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from scale_optimization.config import (
    ALLOWED_LABEL_KEYS,
    FORBIDDEN_LABEL_KEYS,
    MAX_LABEL_CARDINALITY,
    MIN_SAMPLE_COUNT,
    SCHEMA_VERSION,
)
from scale_optimization.errors import INVALID_LABEL, INVALID_METRIC, ScaleOptimizationError
from scale_optimization.models import LatencyBreakdown, MetricPoint, PercentileStats


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_labels(labels: dict[str, Any] | None) -> dict[str, str]:
    raw = dict(labels or {})
    if len(raw) > MAX_LABEL_CARDINALITY:
        raise ScaleOptimizationError(INVALID_LABEL, "cardinality_exceeded")
    out: dict[str, str] = {}
    for key, value in raw.items():
        k = str(key).lower()
        if k in FORBIDDEN_LABEL_KEYS:
            raise ScaleOptimizationError(INVALID_LABEL, f"forbidden:{k}")
        if k not in ALLOWED_LABEL_KEYS:
            raise ScaleOptimizationError(INVALID_LABEL, f"unbounded:{k}")
        text = str(value)
        if len(text) > 64:
            text = text[:64]
        out[k] = text
    return out


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if p < 0 or p > 100:
        raise ScaleOptimizationError(INVALID_METRIC, "percentile_out_of_range")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def aggregate_percentiles(values: list[float]) -> PercentileStats:
    if not values:
        return PercentileStats(count=0, p50=0.0, p95=0.0, p99=0.0, avg=0.0, max=0.0)
    return PercentileStats(
        count=len(values),
        p50=round(percentile(values, 50), 4),
        p95=round(percentile(values, 95), 4),
        p99=round(percentile(values, 99), 4),
        avg=round(sum(values) / len(values), 4),
        max=round(max(values), 4),
    )


class MetricRegistry:
    """In-process metric store with bounded labels (reuses process-local pattern)."""

    def __init__(self):
        self._points: list[MetricPoint] = []
        self._series: dict[str, list[float]] = {}
        self._counters: dict[str, float] = {}

    def emit(self, name: str, value: float, *, labels: dict[str, Any] | None = None, unit: str = "") -> MetricPoint:
        safe = validate_labels(labels)
        point = MetricPoint(name=name, value=float(value), timestamp=_utc_now(), labels=safe, unit=unit)
        self._points.append(point)
        key = self._series_key(name, safe)
        self._series.setdefault(key, []).append(float(value))
        return point

    def incr(self, name: str, amount: float = 1.0, *, labels: dict[str, Any] | None = None) -> float:
        safe = validate_labels(labels)
        key = self._series_key(name, safe)
        self._counters[key] = self._counters.get(key, 0.0) + float(amount)
        self.emit(name, self._counters[key], labels=safe)
        return self._counters[key]

    def series(self, name: str, *, labels: dict[str, Any] | None = None) -> list[float]:
        safe = validate_labels(labels) if labels is not None else {}
        if labels is None:
            # Aggregate all series for name
            out: list[float] = []
            prefix = f"{name}|"
            for key, vals in self._series.items():
                if key.startswith(prefix) or key == name:
                    out.extend(vals)
            return out
        return list(self._series.get(self._series_key(name, safe), []))

    def stats(self, name: str, *, labels: dict[str, Any] | None = None) -> PercentileStats:
        return aggregate_percentiles(self.series(name, labels=labels))

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "point_count": len(self._points),
            "series_count": len(self._series),
            "counters": dict(self._counters),
            "mode": "FIXTURE",
        }

    @staticmethod
    def _series_key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        parts = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
        return f"{name}|{parts}"


def build_latency_breakdown(
    *,
    admission_wait_ms: float = 0.0,
    queue_wait_ms: float = 0.0,
    worker_wait_ms: float = 0.0,
    workflow_time_ms: float = 0.0,
    tool_time_ms: float = 0.0,
    provider_time_ms: float = 0.0,
    persistence_time_ms: float = 0.0,
    response_finalize_time_ms: float = 0.0,
    not_applicable: tuple[str, ...] = (),
) -> LatencyBreakdown:
    total = (
        admission_wait_ms
        + queue_wait_ms
        + worker_wait_ms
        + workflow_time_ms
        + tool_time_ms
        + provider_time_ms
        + persistence_time_ms
        + response_finalize_time_ms
    )
    return LatencyBreakdown(
        total_ms=round(total, 4),
        admission_wait_ms=admission_wait_ms,
        queue_wait_ms=queue_wait_ms,
        worker_wait_ms=worker_wait_ms,
        workflow_time_ms=workflow_time_ms,
        tool_time_ms=tool_time_ms,
        provider_time_ms=provider_time_ms,
        persistence_time_ms=persistence_time_ms,
        response_finalize_time_ms=response_finalize_time_ms,
        not_applicable=not_applicable,
    )


def redact_for_export(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip forbidden keys from arbitrary nested dicts for export safety."""
    forbidden = FORBIDDEN_LABEL_KEYS | {"api_key", "access_token", "refresh_token", "password", "secret"}

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: ("[REDACTED]" if str(k).lower() in forbidden else _walk(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(x) for x in obj]
        return obj

    return _walk(payload)


def insufficient_samples(count: int, *, min_samples: int = MIN_SAMPLE_COUNT) -> bool:
    return count < min_samples
