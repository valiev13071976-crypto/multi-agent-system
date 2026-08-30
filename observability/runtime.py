"""Canonical ObservabilityRuntime — single shared ops surface."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from observability.context import ObservabilityContext
from observability.events import (
    InMemoryObservabilitySink,
    ObservabilitySink,
    OperationalEvent,
    make_event,
)
from observability.health import OperationalHealthSnapshot, build_operational_health
from observability.metrics import MetricsCollector


@dataclass(frozen=True)
class RecentError:
    timestamp: datetime
    component: str
    error_code: str
    event_type: str
    correlation_id: str
    trace_id: str


@dataclass
class ObservabilityRuntime:
    sink: ObservabilitySink
    metrics: MetricsCollector
    enabled: bool = True
    max_event_bytes: int = 4096
    recent_errors_max: int = 100
    _contexts: dict[str, ObservabilityContext] = field(default_factory=dict, repr=False)
    _recent_errors: deque = field(default_factory=deque, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _uncertain_side_effects: int = 0
    emit_errors: int = 0

    def __post_init__(self):
        self._recent_errors = deque(maxlen=max(1, int(self.recent_errors_max)))

    def create_context(
        self,
        *,
        correlation_id: str | None = None,
        workflow_id: str = "",
        task_id: str = "",
        actor_ref: str = "",
        tenant_id: str = "",
    ) -> ObservabilityContext:
        if workflow_id:
            with self._lock:
                existing = self._contexts.get(workflow_id)
            if existing is not None:
                # Preserve lineage: do not invent a second correlation/trace.
                return existing.child(
                    task_id=task_id or existing.task_id,
                    actor_ref=actor_ref or None,
                    tenant_id=tenant_id or None,
                )
        ctx = ObservabilityContext.root(
            correlation_id=correlation_id,
            workflow_id=workflow_id,
            task_id=task_id,
            actor_ref=actor_ref,
            tenant_id=tenant_id,
        )
        if workflow_id:
            with self._lock:
                self._contexts.setdefault(workflow_id, ctx)
        return ctx

    def context_for_workflow(self, workflow_id: str) -> ObservabilityContext | None:
        with self._lock:
            return self._contexts.get(workflow_id)

    def bind_workflow_context(
        self, workflow_id: str, ctx: ObservabilityContext
    ) -> ObservabilityContext:
        bound = ctx.with_workflow(workflow_id)
        with self._lock:
            self._contexts[workflow_id] = bound
        return bound

    def child_span(
        self, ctx: ObservabilityContext | None, **kwargs
    ) -> ObservabilityContext:
        if ctx is None:
            return self.create_context(**kwargs)
        return ctx.child(**kwargs)

    def monotonic_ms(self) -> float:
        return time.perf_counter() * 1000.0

    def emit(
        self,
        event_type: str,
        *,
        context: ObservabilityContext | None = None,
        component: str = "",
        status: str = "",
        tool_id: str = "",
        operation: str = "",
        provider: str = "",
        model: str = "",
        duration_ms: int | None = None,
        error_code: str | None = None,
        risk: str = "",
        trust_level: str = "",
        metadata: Mapping | None = None,
        exception_type: str | None = None,
        update_metrics: bool = True,
    ) -> OperationalEvent | None:
        if not self.enabled:
            return None
        ctx = context or self.create_context()
        try:
            meta = dict(metadata or {})
            if ctx.tenant_id and "tenant_id" not in meta:
                meta["tenant_id"] = ctx.tenant_id
            if ctx.actor_ref and "actor_ref" not in meta:
                meta["actor_ref"] = ctx.actor_ref
            if ctx.correlation_id and "request_id" not in meta:
                # correlation_id is bound to security request_id when provided.
                meta["request_id"] = ctx.correlation_id
            event = make_event(
                event_type,
                correlation_id=ctx.correlation_id,
                trace_id=ctx.trace_id,
                span_id=ctx.span_id,
                parent_span_id=ctx.parent_span_id,
                workflow_id=ctx.workflow_id,
                task_id=ctx.task_id,
                tool_id=tool_id,
                operation=operation,
                provider=provider,
                model=model,
                component=component,
                status=status,
                duration_ms=duration_ms,
                error_code=error_code,
                risk=risk,
                trust_level=trust_level,
                metadata=meta,
                exception_type=exception_type,
                max_bytes=self.max_event_bytes,
            )
            self.sink.emit(event)
            if error_code:
                with self._lock:
                    self._recent_errors.append(
                        RecentError(
                            timestamp=event.timestamp,
                            component=component or event_type.split(".", 1)[0],
                            error_code=str(error_code),
                            event_type=event_type,
                            correlation_id=event.correlation_id,
                            trace_id=event.trace_id,
                        )
                    )
            if update_metrics:
                self._apply_metrics(event)
            return event
        except Exception:
            self.emit_errors += 1
            return None

    def _apply_metrics(self, event: OperationalEvent) -> None:
        et = event.event_type
        m = self.metrics
        try:
            if et == "workflow.created":
                m.inc("workflow_total", labels={"component": "workflow"})
            elif et == "workflow.completed":
                m.inc("workflow_success_total", labels={"component": "workflow", "status": "completed"})
                if event.duration_ms is not None:
                    m.observe_latency("workflow", event.duration_ms)
            elif et == "workflow.failed":
                m.inc("workflow_failure_total", labels={"component": "workflow", "status": "failed"})
            elif et == "tool.requested":
                pass  # counted on terminal outcomes to avoid double-count with P8
            elif et == "tool.completed":
                m.record_tool(
                    tool_id=event.tool_id or "unknown",
                    operation=event.operation or "unknown",
                    trust_level=event.trust_level or "unknown",
                    outcome="success",
                    latency_ms=int(event.duration_ms or 0),
                )
            elif et == "tool.denied":
                m.record_tool(
                    tool_id=event.tool_id or "unknown",
                    operation=event.operation or "unknown",
                    trust_level=event.trust_level or "unknown",
                    outcome="denied",
                    latency_ms=int(event.duration_ms or 0),
                )
            elif et == "tool.uncertain":
                m.record_tool(
                    tool_id=event.tool_id or "unknown",
                    operation=event.operation or "unknown",
                    trust_level=event.trust_level or "unknown",
                    outcome="uncertain",
                    latency_ms=int(event.duration_ms or 0),
                )
            elif et == "tool.failed":
                outcome = "timeout" if event.error_code == "tool_timeout" else "failure"
                m.record_tool(
                    tool_id=event.tool_id or "unknown",
                    operation=event.operation or "unknown",
                    trust_level=event.trust_level or "unknown",
                    outcome=outcome,
                    latency_ms=int(event.duration_ms or 0),
                )
            elif et == "side_effect.started":
                m.inc("side_effect_total", labels={"component": "side_effect"})
            elif et == "side_effect.completed":
                m.inc(
                    "side_effect_success_total",
                    labels={"component": "side_effect", "status": "succeeded"},
                )
                if event.duration_ms is not None:
                    m.observe_latency("side_effect", event.duration_ms)
            elif et == "side_effect.failed":
                m.inc(
                    "side_effect_failure_total",
                    labels={"component": "side_effect", "status": "failed"},
                )
            elif et == "side_effect.uncertain":
                m.inc(
                    "side_effect_uncertain_total",
                    labels={"component": "side_effect", "status": "uncertain"},
                )
                with self._lock:
                    self._uncertain_side_effects += 1
            elif et == "hitl.requested":
                m.inc("approval_requested_total", labels={"component": "hitl"})
            elif et == "hitl.approved":
                m.inc("approval_approved_total", labels={"component": "hitl"})
            elif et == "hitl.rejected":
                m.inc("approval_rejected_total", labels={"component": "hitl"})
            elif et == "permit.issued":
                m.inc("permit_issued_total", labels={"component": "permit"})
            elif et == "permit.consumed":
                m.inc("permit_consumed_total", labels={"component": "permit"})
            elif et == "permit.denied":
                m.inc("permit_denied_total", labels={"component": "permit"})
            elif et == "queue.enqueued":
                m.inc("queue_enqueued_total", labels={"component": "queue"})
            elif et == "queue.dead_lettered":
                m.inc("queue_dead_letter_total", labels={"component": "queue"})
            elif et == "provider.selected":
                m.inc(
                    "provider_calls_total",
                    labels={
                        "component": "provider",
                        "provider": event.provider or "unknown",
                    },
                )
            elif et == "provider.completed":
                if event.duration_ms is not None:
                    m.observe_latency(
                        "provider",
                        event.duration_ms,
                        labels={"component": "provider"},
                    )
            elif et == "provider.failed":
                m.inc("provider_failure_total", labels={"component": "provider"})
            elif et == "finops.budget_denied":
                m.inc("finops_budget_denied_total", labels={"component": "finops"})
            elif et == "validation.completed" and event.duration_ms is not None:
                m.observe_latency("validation", event.duration_ms)
            elif et == "judge.completed" and event.duration_ms is not None:
                m.observe_latency("judge", event.duration_ms)
            elif et == "reconciliation.checked" and event.duration_ms is not None:
                m.observe_latency("reconciliation", event.duration_ms)
        except Exception:
            self.emit_errors += 1

    def recent_errors(self) -> tuple[RecentError, ...]:
        with self._lock:
            return tuple(self._recent_errors)

    def uncertain_side_effect_count(self) -> int:
        with self._lock:
            return int(self._uncertain_side_effects)

    def health(
        self,
        *,
        persistence_ready: bool = True,
        protected_state_ready: bool = True,
        protected_write_required: bool = False,
        active_workflows: int = 0,
        waiting_approval: int = 0,
        pending_reconciliations: int = 0,
        dead_letter_count: int = 0,
        open_recovery_cases: int = 0,
        pending_manual_review: int = 0,
        stale_recovery_jobs: int = 0,
        critical_recovery_blocking: bool = False,
        recovery_persistence_ready: bool = True,
        recovery_required: bool = False,
        memory_status: str = "healthy",
        memory_enabled: bool = False,
        memory_persistence_ready: bool = True,
        document_status: str = "healthy",
        documents_enabled: bool = False,
        document_persistence_ready: bool = True,
        knowledge_status: str = "healthy",
        knowledge_enabled: bool = False,
        knowledge_persistence_ready: bool = True,
        procurement_status: str = "healthy",
        procurement_enabled: bool = False,
        procurement_persistence_ready: bool = True,
    ) -> OperationalHealthSnapshot:
        snap = self.metrics.snapshot()
        return build_operational_health(
            persistence_ready=persistence_ready,
            protected_state_ready=protected_state_ready,
            protected_write_required=protected_write_required,
            active_workflows=active_workflows,
            waiting_approval=waiting_approval,
            uncertain_side_effects=self.uncertain_side_effect_count(),
            pending_reconciliations=pending_reconciliations,
            dead_letter_count=dead_letter_count,
            tool_failures_recent=int(snap.get("tool_failure_total", 0)),
            provider_failures_recent=int(snap.get("provider_failure_total", 0)),
            open_recovery_cases=open_recovery_cases,
            pending_manual_review=pending_manual_review,
            stale_recovery_jobs=stale_recovery_jobs,
            critical_recovery_blocking=critical_recovery_blocking,
            recovery_persistence_ready=recovery_persistence_ready,
            recovery_required=recovery_required,
            memory_status=memory_status,
            memory_enabled=memory_enabled,
            memory_persistence_ready=memory_persistence_ready,
            document_status=document_status,
            documents_enabled=documents_enabled,
            document_persistence_ready=document_persistence_ready,
            knowledge_status=knowledge_status,
            knowledge_enabled=knowledge_enabled,
            knowledge_persistence_ready=knowledge_persistence_ready,
            procurement_status=procurement_status,
            procurement_enabled=procurement_enabled,
            procurement_persistence_ready=procurement_persistence_ready,
        )

    def list_events(self) -> tuple[OperationalEvent, ...]:
        if hasattr(self.sink, "list_events"):
            return self.sink.list_events()
        return ()


def build_observability_runtime(*, env: dict | None = None) -> ObservabilityRuntime:
    source = env if env is not None else os.environ
    enabled = str(source.get("OBSERVABILITY_ENABLED", "true")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    max_events = int(source.get("OBSERVABILITY_MAX_EVENTS", "1000") or 1000)
    max_bytes = int(source.get("OBSERVABILITY_MAX_EVENT_BYTES", "4096") or 4096)
    recent = int(source.get("OBSERVABILITY_RECENT_ERRORS", "100") or 100)
    return ObservabilityRuntime(
        sink=InMemoryObservabilitySink(max_events=max_events),
        metrics=MetricsCollector(),
        enabled=enabled,
        max_event_bytes=max_bytes,
        recent_errors_max=recent,
    )
