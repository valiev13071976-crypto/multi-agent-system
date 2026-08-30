"""Admin read models and audit event contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HEALTH_HEALTHY = "HEALTHY"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_UNHEALTHY = "UNHEALTHY"
HEALTH_UNKNOWN = "UNKNOWN"
HEALTH_STALE = "STALE"

ALERT_INFO = "INFO"
ALERT_WARNING = "WARNING"
ALERT_CRITICAL = "CRITICAL"


@dataclass
class SystemHealthView:
    overall: str
    generated_at: str
    components: list[dict[str, Any]] = field(default_factory=list)
    window: str = "live"


@dataclass
class WorkflowRunView:
    workflow_id: str
    tenant_id: str
    status: str
    workflow_type: str = ""
    created_at: str = ""
    updated_at: str = ""
    error_code: str | None = None
    queue_task_id: str | None = None
    actor_ref: str = ""


@dataclass
class WorkflowRunDetail(WorkflowRunView):
    steps: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueueView:
    lane: str
    depth: int
    active: int
    retry_wait: int
    generated_at: str


@dataclass
class WorkerPoolView:
    pool_name: str
    lane: str
    draining: bool
    health: str
    active_work: int = 0
    capacity: int | None = None


@dataclass
class ProviderHealthView:
    provider: str
    status: str
    breaker_state: str = "unknown"
    throttle_count: int = 0
    latency_ms: float | None = None
    error_rate: float | None = None


@dataclass
class RoutingHealthView:
    status: str
    active_policy_version: str | None = None
    eligible_candidates: int = 0
    degraded_providers: list[str] = field(default_factory=list)
    generated_at: str = ""


@dataclass
class ToolHealthView:
    tool_id: str
    version: str
    status: str
    side_effect: bool = False
    capabilities: tuple[str, ...] = ()


@dataclass
class SideEffectView:
    execution_id: str
    tenant_id: str
    status: str
    tool_id: str = ""
    workflow_id: str = ""
    error_code: str | None = None


@dataclass
class ApprovalQueueView:
    approval_id: str
    workflow_id: str
    tenant_id: str
    status: str
    summary: str
    created_at: str = ""
    expires_at: str | None = None


@dataclass
class UsageSummaryView:
    window: str
    total_cost: str
    total_tokens: int
    by_provider: dict[str, str] = field(default_factory=dict)
    by_model: dict[str, str] = field(default_factory=dict)
    generated_at: str = ""


@dataclass
class BudgetStatusView:
    tenant_id: str
    limit: str
    used: str
    remaining: str
    status: str
    window: str = "monthly"


@dataclass
class TenantUsageView:
    tenant_id: str
    active_runs: int
    queued_jobs: int
    failed_jobs: int
    cost_summary: str = "0"


@dataclass
class FailureView:
    failure_id: str
    tenant_id: str
    operation: str
    error_code: str
    created_at: str
    summary: str = ""


@dataclass
class DLQItemView:
    task_id: str
    tenant_id: str
    operation: str
    status: str
    error_code: str | None
    attempt_count: int
    created_at: str
    redrive_eligible: bool = False
    summary: str = ""


@dataclass
class AuditEventView:
    event_id: str
    timestamp: str
    actor_ref: str
    tenant_scope: str
    capability: str
    action: str
    target_type: str
    target_id: str
    result: str
    reason: str | None = None
    request_id: str | None = None


@dataclass
class AlertView:
    alert_id: str
    severity: str
    source: str
    message: str
    status: str
    first_observed: str
    last_observed: str
    count: int = 1


@dataclass
class CapacityView:
    lane: str
    queue_depth: int
    active_workers: int
    admission_rejections: int
    generated_at: str


@dataclass
class OperationsDashboardView:
    health: SystemHealthView
    active_runs: int
    queued_jobs: int
    failed_jobs: int
    dlq_count: int
    pending_approvals: int
    alerts: list[AlertView]
    cost_summary: UsageSummaryView
    generated_at: str
