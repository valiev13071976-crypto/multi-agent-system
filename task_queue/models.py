from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping


STATUS_QUEUED = "queued"
STATUS_LEASED = "leased"
STATUS_RUNNING = "running"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_COMPLETED = "completed"
STATUS_DEAD_LETTERED = "dead_lettered"
STATUS_CANCELLED = "cancelled"

QUEUE_STATUSES = (
    STATUS_QUEUED,
    STATUS_LEASED,
    STATUS_RUNNING,
    STATUS_RETRY_WAIT,
    STATUS_COMPLETED,
    STATUS_DEAD_LETTERED,
    STATUS_CANCELLED,
)

TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_DEAD_LETTERED, STATUS_CANCELLED}
)

ACTIVE_STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_LEASED, STATUS_RUNNING, STATUS_RETRY_WAIT}
)

PRIORITY_LOW = "low"
PRIORITY_NORMAL = "normal"
PRIORITY_HIGH = "high"
PRIORITY_CRITICAL = "critical"

PRIORITIES = (
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_HIGH,
    PRIORITY_CRITICAL,
)

PRIORITY_RANK = {
    PRIORITY_LOW: 0,
    PRIORITY_NORMAL: 1,
    PRIORITY_HIGH: 2,
    PRIORITY_CRITICAL: 3,
}

ALLOWED_TRANSITIONS = {
    STATUS_QUEUED: frozenset({STATUS_LEASED, STATUS_CANCELLED}),
    STATUS_LEASED: frozenset(
        {STATUS_RUNNING, STATUS_QUEUED, STATUS_CANCELLED, STATUS_COMPLETED}
    ),
    STATUS_RUNNING: frozenset(
        {
            STATUS_COMPLETED,
            STATUS_RETRY_WAIT,
            STATUS_DEAD_LETTERED,
            STATUS_CANCELLED,
        }
    ),
    STATUS_RETRY_WAIT: frozenset({STATUS_LEASED, STATUS_CANCELLED}),
    STATUS_COMPLETED: frozenset(),
    STATUS_DEAD_LETTERED: frozenset(),
    STATUS_CANCELLED: frozenset(),
}

BACKOFF_FIXED = "fixed"
BACKOFF_EXPONENTIAL = "exponential"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class QueueTask:
    queue_task_id: str
    workflow_id: str
    task_id: str
    execution_key: str
    status: str
    priority: str
    attempt: int
    max_attempts: int
    created_at: datetime
    available_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    timeout_seconds: float | None = None
    error_code: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    lease_id: str | None = None
    leased_at: datetime | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self):
        if self.status not in QUEUE_STATUSES:
            raise ValueError(f"Invalid queue status: {self.status!r}")
        if self.priority not in PRIORITIES:
            raise ValueError(f"Invalid queue priority: {self.priority!r}")
        object.__setattr__(self, "metadata", _meta(self.metadata))
