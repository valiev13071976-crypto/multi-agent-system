from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping


STATUS_CREATED = "created"
STATUS_PLANNED = "planned"
STATUS_RUNNING = "running"
STATUS_WAITING_APPROVAL = "waiting_approval"
STATUS_VALIDATING = "validating"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

WORKFLOW_STATUSES = (
    STATUS_CREATED,
    STATUS_PLANNED,
    STATUS_RUNNING,
    STATUS_WAITING_APPROVAL,
    STATUS_VALIDATING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
)

TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}
)

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_WAITING = "waiting"

STEP_STATUSES = (
    STEP_PENDING,
    STEP_RUNNING,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_WAITING,
)

STEP_PREPARE_CONTEXT = "prepare_context"
STEP_ROUTE = "route"
STEP_EXECUTE_EXPERTS = "execute_experts"
STEP_VALIDATE = "validate"
STEP_JUDGE = "judge"
STEP_FORMAT = "format"

ANALYZE_STEPS = (
    STEP_PREPARE_CONTEXT,
    STEP_ROUTE,
    STEP_EXECUTE_EXPERTS,
    STEP_VALIDATE,
    STEP_JUDGE,
    STEP_FORMAT,
)

ALLOWED_TRANSITIONS = {
    STATUS_CREATED: frozenset({STATUS_PLANNED, STATUS_FAILED, STATUS_CANCELLED}),
    STATUS_PLANNED: frozenset({STATUS_RUNNING, STATUS_FAILED, STATUS_CANCELLED}),
    STATUS_RUNNING: frozenset(
        {
            STATUS_VALIDATING,
            STATUS_WAITING_APPROVAL,
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_CANCELLED,
        }
    ),
    STATUS_VALIDATING: frozenset(
        {STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}
    ),
    STATUS_WAITING_APPROVAL: frozenset(
        {STATUS_RUNNING, STATUS_FAILED, STATUS_CANCELLED}
    ),
    STATUS_COMPLETED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_CANCELLED: frozenset(),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class StepRecord:
    step_id: str
    name: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempt: int = 1
    error_code: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in STEP_STATUSES:
            raise ValueError(f"Invalid step status: {self.status!r}")
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class WorkflowState:
    workflow_id: str
    task_id: str
    status: str
    current_step: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    error_code: str | None
    version: int
    steps: tuple[StepRecord, ...]
    execution_key: str

    def __post_init__(self):
        if self.status not in WORKFLOW_STATUSES:
            raise ValueError(f"Invalid workflow status: {self.status!r}")

    def step(self, name: str) -> StepRecord | None:
        for item in self.steps:
            if item.name == name:
                return item
        return None

    def completed_step_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.steps if item.status == STEP_COMPLETED)

    def next_incomplete_step(self) -> str | None:
        for item in self.steps:
            if item.status not in {STEP_COMPLETED, STEP_SKIPPED}:
                return item.name
        return None


@dataclass(frozen=True)
class Checkpoint:
    workflow_id: str
    workflow_version: int
    status: str
    current_step: str | None
    completed_steps: tuple[str, ...]
    timestamp: datetime
    payload: Mapping[str, object]
    sensitivity: str = "internal"

    def __post_init__(self):
        object.__setattr__(self, "payload", _meta(self.payload))
        object.__setattr__(self, "completed_steps", tuple(self.completed_steps))
