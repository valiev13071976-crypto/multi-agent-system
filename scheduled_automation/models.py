"""Scheduled automation domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEDULE_ONCE = "ONCE"
SCHEDULE_INTERVAL = "INTERVAL"
SCHEDULE_DAILY = "DAILY"
SCHEDULE_WEEKLY = "WEEKLY"

SCHEDULE_TYPES = frozenset({SCHEDULE_ONCE, SCHEDULE_INTERVAL, SCHEDULE_DAILY, SCHEDULE_WEEKLY})

MISFIRE_SKIP = "SKIP"
MISFIRE_RUN_ONCE = "RUN_ONCE"
MISFIRE_CATCH_UP_BOUNDED = "CATCH_UP_BOUNDED"

OVERLAP_ALLOW = "ALLOW"
OVERLAP_FORBID = "FORBID"

TARGET_WORKFLOW = "WORKFLOW"
TARGET_BA_REQUEST = "BUSINESS_ASSISTANT_REQUEST"
TARGET_ANALYTICS = "ANALYTICS_QUERY"
TARGET_INTEGRATION_READ = "INTEGRATION_READ"

ALLOWED_TARGETS = frozenset({TARGET_WORKFLOW, TARGET_BA_REQUEST, TARGET_ANALYTICS, TARGET_INTEGRATION_READ})

WORKLOAD_NORMAL = "NORMAL"
WORKLOAD_BACKGROUND = "BACKGROUND"
WORKLOAD_BATCH = "BATCH"

OCC_PENDING = "PENDING"
OCC_CLAIMED = "CLAIMED"
OCC_DISPATCHED = "DISPATCHED"
OCC_RUNNING = "RUNNING"
OCC_WAITING_APPROVAL = "WAITING_APPROVAL"
OCC_SUCCEEDED = "SUCCEEDED"
OCC_FAILED = "FAILED"
OCC_SKIPPED = "SKIPPED"
OCC_CANCELLED = "CANCELLED"
OCC_BLOCKED = "BLOCKED"


@dataclass
class ScheduleDefinition:
    schedule_id: str
    tenant_id: str
    owner_id: str
    name: str
    enabled: bool
    paused: bool
    schedule_type: str
    timezone: str
    start_at: str
    end_at: str | None
    interval_seconds: int | None
    daily_time: str | None
    weekly_day: int | None
    max_occurrences: int | None
    misfire_policy: str
    overlap_policy: str
    workload_class: str
    priority: int
    target_type: str
    target_payload: dict[str, Any]
    required_capabilities: tuple[str, ...]
    version: int
    occurrence_count: int
    failure_count: int
    next_run_at: str | None
    last_dispatch_at: str | None
    last_run_id: str | None
    created_at: str
    updated_at: str
    created_by: str
    metadata: dict[str, Any] = field(default_factory=dict)
    archived: bool = False


@dataclass
class ScheduleOccurrence:
    occurrence_id: str
    schedule_id: str
    tenant_id: str
    schedule_version: int
    scheduled_for: str
    status: str
    execution_key: str
    run_id: str | None = None
    claimed_at: str | None = None
    dispatched_at: str | None = None
    completed_at: str | None = None
    error_code: str | None = None
    manual: bool = False
