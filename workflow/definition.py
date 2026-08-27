"""Immutable workflow domain contracts for durable DAG execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from task_queue.retry import (
    BACKOFF_EXPONENTIAL,
    BACKOFF_FIXED,
    RetryPolicy as QueueRetryPolicy,
)


FAILURE_FAIL_WORKFLOW = "fail_workflow"
FAILURE_RETRY = "retry"
FAILURE_SKIP = "skip"
FAILURE_CONTINUE = "continue"
FAILURE_WAIT_FOR_HUMAN = "wait_for_human"
FAILURE_COMPENSATE = "compensate"

FAILURE_POLICIES = frozenset(
    {
        FAILURE_FAIL_WORKFLOW,
        FAILURE_RETRY,
        FAILURE_SKIP,
        FAILURE_CONTINUE,
        FAILURE_WAIT_FOR_HUMAN,
        FAILURE_COMPENSATE,
    }
)

STEP_TYPE_HANDLER = "handler"
STEP_TYPE_SIDE_EFFECT = "side_effect"
STEP_TYPE_BRANCH = "branch"
STEP_TYPE_VALIDATE = "validate"
STEP_TYPE_APPROVAL = "approval"

BRANCH_OP_EQ = "eq"
BRANCH_OP_NE = "ne"
BRANCH_OP_IN = "in"
BRANCH_OP_EXISTS = "exists"
BRANCH_OP_TRUTHY = "truthy"
BRANCH_OP_FALSY = "falsy"

BRANCH_OPS = frozenset(
    {
        BRANCH_OP_EQ,
        BRANCH_OP_NE,
        BRANCH_OP_IN,
        BRANCH_OP_EXISTS,
        BRANCH_OP_TRUTHY,
        BRANCH_OP_FALSY,
    }
)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class StepRetryPolicy:
    max_attempts: int = 1
    base_delay_seconds: float = 5.0
    max_delay_seconds: float = 60.0
    backoff_mode: str = BACKOFF_FIXED
    retryable_error_classes: tuple[str, ...] = ()
    non_retryable_error_classes: tuple[str, ...] = ()

    def __post_init__(self):
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_mode not in {BACKOFF_FIXED, BACKOFF_EXPONENTIAL}:
            raise ValueError(f"Invalid backoff_mode: {self.backoff_mode!r}")
        object.__setattr__(
            self, "retryable_error_classes", tuple(self.retryable_error_classes)
        )
        object.__setattr__(
            self,
            "non_retryable_error_classes",
            tuple(self.non_retryable_error_classes),
        )

    def to_queue_policy(self) -> QueueRetryPolicy:
        return QueueRetryPolicy(
            max_attempts=self.max_attempts,
            base_delay_seconds=self.base_delay_seconds,
            max_delay_seconds=self.max_delay_seconds,
            backoff_mode=self.backoff_mode,
        )

    def delay_seconds(self, attempt: int) -> float:
        return self.to_queue_policy().delay_seconds(attempt)


@dataclass(frozen=True)
class BranchCondition:
    """Structured, non-code branch predicate over prior step results."""

    source_step_id: str
    field: str
    op: str
    value: object = None

    def __post_init__(self):
        if self.op not in BRANCH_OPS:
            raise ValueError(f"Invalid branch op: {self.op!r}")
        if not self.source_step_id:
            raise ValueError("source_step_id required")
        if not self.field and self.op not in {BRANCH_OP_TRUTHY, BRANCH_OP_FALSY}:
            # empty field means whole result dict for truthy/falsy only
            if self.op not in {BRANCH_OP_EXISTS}:
                raise ValueError("field required for branch condition")


@dataclass(frozen=True)
class BranchRule:
    """Deterministic branch: when condition matches, activate then_steps; else else_steps."""

    condition: BranchCondition
    then_steps: tuple[str, ...] = ()
    else_steps: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "then_steps", tuple(self.then_steps))
        object.__setattr__(self, "else_steps", tuple(self.else_steps))


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    step_type: str
    dependencies: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    retry_policy: StepRetryPolicy = field(default_factory=StepRetryPolicy)
    failure_policy: str = FAILURE_FAIL_WORKFLOW
    requires_approval: bool = False
    compensation_action: str | None = None
    branch: BranchRule | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not self.step_id:
            raise ValueError("step_id required")
        if self.failure_policy not in FAILURE_POLICIES:
            raise ValueError(f"Invalid failure_policy: {self.failure_policy!r}")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_type: str
    version: str
    steps: tuple[WorkflowStep, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)
    timeout_seconds: float | None = None

    def __post_init__(self):
        if not self.workflow_type:
            raise ValueError("workflow_type required")
        if not self.version:
            raise ValueError("version required")
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        ids = [s.step_id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step_id in WorkflowDefinition")

    def step(self, step_id: str) -> WorkflowStep | None:
        for item in self.steps:
            if item.step_id == step_id:
                return item
        return None

    def step_ids(self) -> tuple[str, ...]:
        return tuple(s.step_id for s in self.steps)

    @property
    def key(self) -> str:
        return f"{self.workflow_type}@{self.version}"


@dataclass(frozen=True)
class WorkflowInstance:
    workflow_id: str
    workflow_type: str
    version: str
    status: str
    current_steps: tuple[str, ...]
    ready_steps: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    checkpoint_version: int
    error_code: str | None
    next_retry_at: datetime | None = None
    deadline_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "current_steps", tuple(self.current_steps))
        object.__setattr__(self, "ready_steps", tuple(self.ready_steps))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class StepExecution:
    workflow_id: str
    step_id: str
    attempt: int
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    result_ref: str | None = None
    error_code: str | None = None
    error_class: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class StepResult:
    ok: bool
    data: Mapping[str, object] = field(default_factory=dict)
    error_code: str | None = None
    error_class: str | None = None
    result_ref: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "data", _meta(self.data))


@dataclass(frozen=True)
class ScheduleSpec:
    """One-time or interval schedule. Cron-like = interval seconds only (no expression eval)."""

    schedule_id: str
    workflow_type: str
    version: str
    payload: Mapping[str, object] = field(default_factory=dict)
    run_at: datetime | None = None
    interval_seconds: float | None = None
    enabled: bool = True
    execution_key_prefix: str = "schedule"

    def __post_init__(self):
        if not self.schedule_id:
            raise ValueError("schedule_id required")
        if self.run_at is None and self.interval_seconds is None:
            raise ValueError("run_at or interval_seconds required")
        if self.interval_seconds is not None and float(self.interval_seconds) <= 0:
            raise ValueError("interval_seconds must be > 0")
        object.__setattr__(self, "payload", _meta(self.payload))
