"""Controlled automation domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Definition states
STATE_DRAFT = "DRAFT"
STATE_ENABLED = "ENABLED"
STATE_PAUSED = "PAUSED"
STATE_DISABLED = "DISABLED"
STATE_BLOCKED = "BLOCKED"
STATE_ARCHIVED = "ARCHIVED"

DEFINITION_STATES = frozenset({STATE_DRAFT, STATE_ENABLED, STATE_PAUSED, STATE_DISABLED, STATE_BLOCKED, STATE_ARCHIVED})

# Run states
RUN_PENDING = "PENDING"
RUN_EVALUATING = "EVALUATING"
RUN_NO_ACTION = "NO_ACTION"
RUN_PREPARED = "PREPARED"
RUN_WAITING_APPROVAL = "WAITING_APPROVAL"
RUN_EXECUTING = "EXECUTING"
RUN_VERIFYING = "VERIFYING"
RUN_SUCCEEDED = "SUCCEEDED"
RUN_PARTIAL = "PARTIAL"
RUN_FAILED = "FAILED"
RUN_BLOCKED = "BLOCKED"
RUN_UNKNOWN_EXTERNAL = "UNKNOWN_EXTERNAL_STATE"

# Triggers
TRIGGER_TIME = "TIME"
TRIGGER_SCHEDULE = "SCHEDULE"
TRIGGER_BUSINESS_EVENT = "BUSINESS_EVENT"
TRIGGER_MANUAL = "MANUAL"
TRIGGER_CONDITION_CHECK = "CONDITION_CHECK"

TRIGGER_TYPES = frozenset({TRIGGER_TIME, TRIGGER_SCHEDULE, TRIGGER_BUSINESS_EVENT, TRIGGER_MANUAL, TRIGGER_CONDITION_CHECK})

# Data quality
DATA_KNOWN = "KNOWN"
DATA_UNKNOWN = "UNKNOWN"
DATA_STALE = "STALE"
DATA_PARTIAL = "PARTIAL"
DATA_ERROR = "ERROR"

# Allowed actions
ALLOWED_ACTIONS = frozenset({
    "ANALYTICS_READ",
    "STOCK_READ",
    "PRICE_READ",
    "PREPARE_PRICE_UPDATE",
    "PREPARE_PRODUCT_UPDATE",
    "PREPARE_MARKETPLACE_CARD",
    "PREPARE_EMAIL",
    "PREPARE_CALENDAR_EVENT",
    "PREPARE_CRM_UPDATE",
    "PREPARE_BITRIX_UPDATE",
    "CONTENT_GENERATE",
    "SEO_ANALYZE",
    "MARKETPLACE_PRICE_UPDATE",
    "BITRIX_PRODUCT_UPDATE",
    "CRM_UPDATE",
    "CALENDAR_CREATE",
    "EMAIL_SEND",
})

FORBIDDEN_PAYLOAD_KEYS = frozenset({"code", "script", "shell", "eval", "sql", "exec", "callable"})


@dataclass
class PolicyEnvelope:
    allowed_action_types: tuple[str, ...]
    allowed_integration_ids: tuple[str, ...]
    allowed_resource_scope: tuple[str, ...]
    max_actions_per_run: int = 10
    max_actions_per_hour: int = 100
    max_actions_per_day: int = 500
    max_items_per_action: int = 50
    requires_approval: bool = False
    allow_auto_execute: bool = False
    dry_run: bool = False
    valid_from: str | None = None
    valid_until: str | None = None
    cooldown_seconds: int = 300
    min_delta_pct: float | None = None
    kill_switch_scope: str | None = None


@dataclass
class ControlledAutomationDefinition:
    automation_id: str
    tenant_id: str
    owner_id: str
    name: str
    description: str
    enabled: bool
    paused: bool
    state: str
    version: int
    trigger: dict[str, Any]
    conditions: dict[str, Any]
    actions: tuple[dict[str, Any], ...]
    policy: PolicyEnvelope
    risk_class: str
    approval_policy: dict[str, Any]
    budget_policy: dict[str, Any]
    rate_policy: dict[str, Any]
    scope: dict[str, Any]
    required_capabilities: tuple[str, ...]
    schedule_id: str | None
    created_at: str
    updated_at: str
    created_by: str
    last_evaluated_at: str | None = None
    last_executed_at: str | None = None
    archived: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BusinessEvent:
    event_id: str
    event_type: str
    tenant_id: str
    owner_id: str
    occurred_at: str
    source: str
    subject_type: str
    subject_id: str
    payload_ref: str
    trace_id: str
    schema_version: str = "1"
    origin_automation_id: str | None = None
    causation_id: str | None = None


@dataclass
class AutomationRun:
    run_id: str
    automation_id: str
    tenant_id: str
    automation_version: int
    trigger_type: str
    event_id: str | None
    status: str
    execution_key: str
    dry_run: bool
    condition_result: dict[str, Any] = field(default_factory=dict)
    policy_result: dict[str, Any] = field(default_factory=dict)
    actions_planned: tuple[dict[str, Any], ...] = ()
    actions_executed: tuple[dict[str, Any], ...] = ()
    approval_id: str | None = None
    approval_fingerprint: str | None = None
    blocked_reason: str | None = None
    error_code: str | None = None
    started_at: str = ""
    completed_at: str | None = None
    trace_id: str | None = None
    cost: str | None = None
