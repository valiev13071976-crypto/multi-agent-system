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
        "tool_trust_level",
        "provider",
        "model_family",
        "status",
        "error_code_class",
        "scope_type",
        "decision",
        "case_type",
        "severity",
        "memory_type",
        "sensitivity",
        "scope_type",
        "document_type",
        "parser_id",
        "source_type",
        "trust_level",
        "category",
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
    budget_reservations_total: int = 0
    budget_reservation_denied_total: int = 0
    budget_degrade_total: int = 0
    budget_terminate_total: int = 0
    budget_skip_model_total: int = 0
    budget_reserved_amount: int = 0
    budget_spent_amount: int = 0
    budget_released_amount: int = 0
    recovery_cases_total: int = 0
    recovery_open_cases: int = 0
    recovery_resolved_total: int = 0
    recovery_blocked_total: int = 0
    recovery_manual_review_total: int = 0
    recovery_read_checks_total: int = 0
    recovery_read_check_failures_total: int = 0
    memory_ingest_total: int = 0
    memory_retrieval_total: int = 0
    memory_dedup_total: int = 0
    memory_forget_total: int = 0
    memory_denied_total: int = 0
    document_ingest_total: int = 0
    document_parse_total: int = 0
    document_parse_failure_total: int = 0
    document_chunk_total: int = 0
    document_dedup_total: int = 0
    document_bytes_total: int = 0
    spreadsheet_sheet_total: int = 0
    spreadsheet_range_extract_total: int = 0
    knowledge_ingest_total: int = 0
    knowledge_retrieval_total: int = 0
    knowledge_refresh_total: int = 0
    knowledge_refresh_failure_total: int = 0
    knowledge_stale_result_total: int = 0
    knowledge_denied_total: int = 0
    procurement_requests_total: int = 0
    procurement_suppliers_considered_total: int = 0
    procurement_offers_total: int = 0
    procurement_offer_rejected_total: int = 0
    procurement_recommendations_total: int = 0
    procurement_approval_required_total: int = 0
    procurement_failures_total: int = 0
    procurement_external_search_total: int = 0
    procurement_external_search_failure_total: int = 0
    procurement_catalog_read_total: int = 0
    procurement_catalog_read_failure_total: int = 0
    procurement_rfq_draft_total: int = 0
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
                "recovery_cases_total": self.recovery_cases_total,
                "recovery_open_cases": self.recovery_open_cases,
                "recovery_resolved_total": self.recovery_resolved_total,
                "recovery_blocked_total": self.recovery_blocked_total,
                "recovery_manual_review_total": self.recovery_manual_review_total,
                "recovery_read_checks_total": self.recovery_read_checks_total,
                "recovery_read_check_failures_total": self.recovery_read_check_failures_total,
                "memory_ingest_total": self.memory_ingest_total,
                "memory_retrieval_total": self.memory_retrieval_total,
                "memory_dedup_total": self.memory_dedup_total,
                "memory_forget_total": self.memory_forget_total,
                "memory_denied_total": self.memory_denied_total,
                "document_ingest_total": self.document_ingest_total,
                "document_parse_total": self.document_parse_total,
                "document_parse_failure_total": self.document_parse_failure_total,
                "document_chunk_total": self.document_chunk_total,
                "document_dedup_total": self.document_dedup_total,
                "document_bytes_total": self.document_bytes_total,
                "spreadsheet_sheet_total": self.spreadsheet_sheet_total,
                "spreadsheet_range_extract_total": self.spreadsheet_range_extract_total,
                "knowledge_ingest_total": self.knowledge_ingest_total,
                "knowledge_retrieval_total": self.knowledge_retrieval_total,
                "knowledge_refresh_total": self.knowledge_refresh_total,
                "knowledge_refresh_failure_total": self.knowledge_refresh_failure_total,
                "knowledge_stale_result_total": self.knowledge_stale_result_total,
                "knowledge_denied_total": self.knowledge_denied_total,
                "procurement_requests_total": self.procurement_requests_total,
                "procurement_suppliers_considered_total": self.procurement_suppliers_considered_total,
                "procurement_offers_total": self.procurement_offers_total,
                "procurement_offer_rejected_total": self.procurement_offer_rejected_total,
                "procurement_recommendations_total": self.procurement_recommendations_total,
                "procurement_approval_required_total": self.procurement_approval_required_total,
                "procurement_failures_total": self.procurement_failures_total,
                "procurement_external_search_total": self.procurement_external_search_total,
                "procurement_external_search_failure_total": self.procurement_external_search_failure_total,
                "procurement_catalog_read_total": self.procurement_catalog_read_total,
                "procurement_catalog_read_failure_total": self.procurement_catalog_read_failure_total,
                "procurement_rfq_draft_total": self.procurement_rfq_draft_total,
                "latency": latency,
                "by_label": by_label,
            }
