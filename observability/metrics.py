"""Canonical MetricsCollector — low-cardinality labels only."""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field


ALLOWED_LABEL_KEYS = frozenset(
    {
        "component",
        "tool_id",
        "operation",
        "trust_level",
        "provider",
        "model_family",
        "status",
        "error_code_class",
    }
)

FORBIDDEN_LABEL_KEYS = frozenset(
    {
        "workflow_id",
        "task_id",
        "actor_id",
        "repo",
        "issue",
        "issue_number",
        "customer",
        "url",
        "raw_url",
        "prompt",
        "external_reference",
        "correlation_id",
        "trace_id",
        "span_id",
    }
)


class HighCardinalityLabelError(ValueError):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"high_cardinality_label:{key}")


def _label_key(labels: dict[str, str]) -> tuple[str, ...]:
    return tuple(f"{k}={labels[k]}" for k in sorted(labels))


@dataclass
class MetricsCollector:
    workflow_total: int = 0
    workflow_success_total: int = 0
    workflow_failure_total: int = 0
    tool_calls_total: int = 0
    tool_success_total: int = 0
    tool_failure_total: int = 0
    tool_denied_total: int = 0
    tool_uncertain_total: int = 0
    tool_timeout_total: int = 0
    side_effect_total: int = 0
    side_effect_success_total: int = 0
    side_effect_failure_total: int = 0
    side_effect_uncertain_total: int = 0
    approval_requested_total: int = 0
    approval_approved_total: int = 0
    approval_rejected_total: int = 0
    permit_issued_total: int = 0
    permit_consumed_total: int = 0
    permit_denied_total: int = 0
    queue_enqueued_total: int = 0
    queue_dead_letter_total: int = 0
    provider_calls_total: int = 0
    provider_failure_total: int = 0
    finops_budget_denied_total: int = 0
    # latency_ms sums + counts by name
    latency: dict[str, dict[str, float]] = field(
        default_factory=lambda: defaultdict(lambda: {"sum_ms": 0.0, "count": 0.0})
    )
    by_label: dict[str, dict[tuple[str, ...], int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def validate_labels(self, labels: dict | None) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, value in dict(labels or {}).items():
            name = str(key)
            if name in FORBIDDEN_LABEL_KEYS:
                raise HighCardinalityLabelError(name)
            if name not in ALLOWED_LABEL_KEYS:
                raise HighCardinalityLabelError(name)
            cleaned[name] = str(value)[:64]
        return cleaned

    def inc(self, counter: str, *, labels: dict | None = None, amount: int = 1) -> None:
        safe = self.validate_labels(labels)
        with self._lock:
            if not hasattr(self, counter):
                raise AttributeError(counter)
            current = getattr(self, counter)
            setattr(self, counter, int(current) + int(amount))
            if safe:
                self.by_label[counter][_label_key(safe)] += int(amount)

    def observe_latency(
        self, name: str, duration_ms: float, *, labels: dict | None = None
    ) -> None:
        safe = self.validate_labels(labels)
        key = str(name)
        with self._lock:
            bucket = self.latency[key]
            bucket["sum_ms"] = float(bucket["sum_ms"]) + float(duration_ms)
            bucket["count"] = float(bucket["count"]) + 1.0
            if safe:
                self.by_label[f"latency:{key}"][_label_key(safe)] += 1

    def record_tool(
        self,
        *,
        tool_id: str,
        operation: str,
        trust_level: str,
        outcome: str,
        latency_ms: int = 0,
    ) -> None:
        labels = {
            "tool_id": tool_id,
            "operation": operation,
            "trust_level": trust_level,
            "component": "tool_gateway",
        }
        self.inc("tool_calls_total", labels=labels)
        if outcome == "success":
            self.inc("tool_success_total", labels=labels)
        elif outcome == "denied":
            self.inc("tool_denied_total", labels=labels)
        elif outcome == "timeout":
            self.inc("tool_timeout_total", labels=labels)
            self.inc("tool_failure_total", labels={**labels, "status": "timeout"})
        elif outcome == "uncertain":
            self.inc("tool_uncertain_total", labels=labels)
        else:
            self.inc("tool_failure_total", labels=labels)
        if latency_ms:
            self.observe_latency("tool", latency_ms, labels=labels)

    def snapshot(self) -> dict:
        with self._lock:
            latency = {k: dict(v) for k, v in self.latency.items()}
            by_label = {
                name: {"|".join(k): n for k, n in buckets.items()}
                for name, buckets in self.by_label.items()
            }
            return {
                "workflow_total": self.workflow_total,
                "workflow_success_total": self.workflow_success_total,
                "workflow_failure_total": self.workflow_failure_total,
                "tool_calls_total": self.tool_calls_total,
                "tool_success_total": self.tool_success_total,
                "tool_failure_total": self.tool_failure_total,
                "tool_denied_total": self.tool_denied_total,
                "tool_uncertain_total": self.tool_uncertain_total,
                "tool_timeout_total": self.tool_timeout_total,
                "side_effect_total": self.side_effect_total,
                "side_effect_success_total": self.side_effect_success_total,
                "side_effect_failure_total": self.side_effect_failure_total,
                "side_effect_uncertain_total": self.side_effect_uncertain_total,
                "approval_requested_total": self.approval_requested_total,
                "approval_approved_total": self.approval_approved_total,
                "approval_rejected_total": self.approval_rejected_total,
                "permit_issued_total": self.permit_issued_total,
                "permit_consumed_total": self.permit_consumed_total,
                "permit_denied_total": self.permit_denied_total,
                "queue_enqueued_total": self.queue_enqueued_total,
                "queue_dead_letter_total": self.queue_dead_letter_total,
                "provider_calls_total": self.provider_calls_total,
                "provider_failure_total": self.provider_failure_total,
                "finops_budget_denied_total": self.finops_budget_denied_total,
                "latency": latency,
                "by_label": by_label,
            }
