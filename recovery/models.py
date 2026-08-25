"""P12 Failure Recovery Orchestration models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata


RECOVERY_POLICY_VERSION = "1.0.0"

CASE_UNCERTAIN_SIDE_EFFECT = "uncertain_side_effect"
CASE_STALE_STARTED = "stale_started_execution"
CASE_PENDING_RECONCILIATION = "pending_reconciliation"
CASE_MANUAL_REVIEW = "manual_review_required"
CASE_PERMIT_CONSUMED_BEFORE_MUTATION = "permit_consumed_before_mutation"
CASE_BUDGET_UNCERTAIN_COST = "budget_uncertain_cost"
CASE_WORKFLOW_WAITING_RECOVERY = "workflow_waiting_recovery"
CASE_TYPES = (
    CASE_UNCERTAIN_SIDE_EFFECT,
    CASE_STALE_STARTED,
    CASE_PENDING_RECONCILIATION,
    CASE_MANUAL_REVIEW,
    CASE_PERMIT_CONSUMED_BEFORE_MUTATION,
    CASE_BUDGET_UNCERTAIN_COST,
    CASE_WORKFLOW_WAITING_RECOVERY,
)

STATUS_OPEN = "open"
STATUS_QUEUED = "queued"
STATUS_CHECKING = "checking"
STATUS_WAITING_OPERATOR = "waiting_operator"
STATUS_WAITING_APPROVAL = "waiting_approval"
STATUS_RESOLVED = "resolved"
STATUS_BLOCKED = "blocked"
STATUS_CANCELLED = "cancelled"
CASE_STATUSES = (
    STATUS_OPEN,
    STATUS_QUEUED,
    STATUS_CHECKING,
    STATUS_WAITING_OPERATOR,
    STATUS_WAITING_APPROVAL,
    STATUS_RESOLVED,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
)
ACTIVE_CASE_STATUSES = frozenset(
    {
        STATUS_OPEN,
        STATUS_QUEUED,
        STATUS_CHECKING,
        STATUS_WAITING_OPERATOR,
        STATUS_WAITING_APPROVAL,
    }
)
TERMINAL_CASE_STATUSES = frozenset(
    {STATUS_RESOLVED, STATUS_BLOCKED, STATUS_CANCELLED}
)

ACTION_RECONCILE_READ_ONLY = "reconcile_read_only"
ACTION_RESUME_WORKFLOW = "resume_workflow"
ACTION_REQUEST_NEW_AUTHORIZATION = "request_new_authorization"
ACTION_ROLLBACK = "rollback"
ACTION_MARK_RESOLVED = "mark_resolved"
ACTION_MARK_BLOCKED = "mark_blocked"
ACTION_DEFER = "defer"
ACTION_CANCEL = "cancel"
ACTION_TYPES = (
    ACTION_RECONCILE_READ_ONLY,
    ACTION_RESUME_WORKFLOW,
    ACTION_REQUEST_NEW_AUTHORIZATION,
    ACTION_ROLLBACK,
    ACTION_MARK_RESOLVED,
    ACTION_MARK_BLOCKED,
    ACTION_DEFER,
    ACTION_CANCEL,
)

DECISION_RECONCILE = "RECONCILE"
DECISION_RESUME = "RESUME"
DECISION_ROLLBACK = "ROLLBACK"
DECISION_DEFER = "DEFER"
DECISION_BLOCK = "BLOCK"
DECISION_CANCEL = "CANCEL"
OPERATOR_DECISIONS = (
    DECISION_RECONCILE,
    DECISION_RESUME,
    DECISION_ROLLBACK,
    DECISION_DEFER,
    DECISION_BLOCK,
    DECISION_CANCEL,
)

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_NORMAL = "normal"
SEVERITY_LOW = "low"
SEVERITIES = (SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_NORMAL, SEVERITY_LOW)

QUEUE_PENDING = "pending"
QUEUE_LEASED = "leased"
QUEUE_COMPLETED = "completed"
QUEUE_DEFERRED = "deferred"
QUEUE_CANCELLED = "cancelled"
QUEUE_DEAD = "dead_letter"
QUEUE_STATUSES = (
    QUEUE_PENDING,
    QUEUE_LEASED,
    QUEUE_COMPLETED,
    QUEUE_DEFERRED,
    QUEUE_CANCELLED,
    QUEUE_DEAD,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


@dataclass(frozen=True)
class RecoveryCase:
    recovery_id: str
    execution_id: str
    workflow_id: str
    task_id: str
    action_id: str
    tool_id: str
    operation: str
    case_type: str
    status: str
    severity: str
    reason_code: str
    created_at: datetime
    updated_at: datetime
    next_check_at: datetime | None = None
    attempt: int = 0
    max_attempts: int = 3
    operator_decision: str | None = None
    reconciliation_id: str | None = None
    parent_recovery_id: str | None = None
    tool_trust_level: str = ""
    reversible: bool = False
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self):
        if self.case_type not in CASE_TYPES:
            raise ValueError(f"invalid_case_type:{self.case_type}")
        if self.status not in CASE_STATUSES:
            raise ValueError(f"invalid_case_status:{self.status}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"invalid_severity:{self.severity}")
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class RecoveryAction:
    action_type: str
    reason_code: str = ""
    requires_authorization: bool = False
    mutates: bool = False
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.action_type not in ACTION_TYPES:
            raise ValueError(f"invalid_recovery_action:{self.action_type}")
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class RecoveryDecision:
    decision_id: str
    recovery_id: str
    decision: str
    actor_id: str
    reason_code: str
    created_at: datetime
    note_safe: str = ""
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.decision not in OPERATOR_DECISIONS:
            raise ValueError(f"invalid_operator_decision:{self.decision}")
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class RecoveryPlan:
    recovery_id: str
    steps: tuple[RecoveryAction, ...]
    reason_code: str
    waiting_operator: bool = False
    metadata_safe: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))


@dataclass(frozen=True)
class RecoveryQueueJob:
    job_id: str
    recovery_id: str
    action_type: str
    scheduled_at: datetime
    priority: str
    attempt: int
    status: str
    leased_at: datetime | None = None
    completed_at: datetime | None = None
    metadata_safe: Mapping[str, object] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self):
        if self.status not in QUEUE_STATUSES:
            raise ValueError(f"invalid_queue_status:{self.status}")
        if self.action_type not in ACTION_TYPES:
            raise ValueError(f"invalid_queue_action:{self.action_type}")
        object.__setattr__(self, "metadata_safe", _meta(self.metadata_safe))
