"""Business Assistant API contracts — transport layer only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

API_VERSION = "v1"

# Request lifecycle
ST_RECEIVED = "RECEIVED"
ST_VALIDATING = "VALIDATING"
ST_PLANNING = "PLANNING"
ST_QUEUED = "QUEUED"
ST_RUNNING = "RUNNING"
ST_WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
ST_RESUMING = "RESUMING"
ST_COMPLETED = "COMPLETED"
ST_FAILED = "FAILED"
ST_REJECTED = "REJECTED"
ST_CANCELLED = "CANCELLED"
ST_BLOCKED = "BLOCKED"

TERMINAL_STATES = frozenset(
    {ST_COMPLETED, ST_FAILED, ST_REJECTED, ST_CANCELLED, ST_BLOCKED}
)

# Progress events
EV_REQUEST_ACCEPTED = "REQUEST_ACCEPTED"
EV_VALIDATION_STARTED = "VALIDATION_STARTED"
EV_PLAN_CREATED = "PLAN_CREATED"
EV_EXECUTION_STARTED = "EXECUTION_STARTED"
EV_STEP_STARTED = "STEP_STARTED"
EV_STEP_PROGRESS = "STEP_PROGRESS"
EV_STEP_COMPLETED = "STEP_COMPLETED"
EV_PREVIEW_READY = "PREVIEW_READY"
EV_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
EV_APPROVAL_RECEIVED = "APPROVAL_RECEIVED"
EV_RESUME_STARTED = "RESUME_STARTED"
EV_ARTIFACT_CREATED = "ARTIFACT_CREATED"
EV_RESULT_READY = "RESULT_READY"
EV_REQUEST_COMPLETED = "REQUEST_COMPLETED"
EV_REQUEST_FAILED = "REQUEST_FAILED"
EV_REQUEST_CANCELLED = "REQUEST_CANCELLED"
EV_REQUEST_BLOCKED = "REQUEST_BLOCKED"

WORKLOAD_INTERACTIVE = "interactive"
WORKLOAD_BATCH = "batch"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ConversationRecord:
    conversation_id: str
    tenant_id: str
    owner_id: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageRecord:
    message_id: str
    conversation_id: str
    tenant_id: str
    role: str
    content: str
    created_at: str
    request_id: str = ""
    artifact_refs: tuple[str, ...] = ()


@dataclass
class ProgressEvent:
    event_id: str
    request_id: str
    tenant_id: str
    event_type: str
    timestamp: str
    workflow_id: str = ""
    stage: str = ""
    step: str = ""
    status: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""


@dataclass
class ApiRequestRecord:
    request_id: str
    tenant_id: str
    owner_id: str
    status: str
    message: str
    created_at: str
    updated_at: str
    conversation_id: str = ""
    idempotency_key: str = ""
    payload_hash: str = ""
    ba_request_id: str = ""
    plan_id: str = ""
    execution_id: str = ""
    workflow_id: str = ""
    correlation_id: str = ""
    trace_id: str = ""
    workload_class: str = WORKLOAD_INTERACTIVE
    artifact_refs: tuple[str, ...] = ()
    read_only: bool = False
    error_code: str = ""
    error_message: str = ""
    finops_cost: str = "0"
    approval_id: str = ""
    preview_id: str = ""
    plan_fingerprint: str = ""
