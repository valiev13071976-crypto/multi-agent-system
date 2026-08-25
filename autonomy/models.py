from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from security.redaction import redact


ACTION_READ = "read"
ACTION_WRITE = "write"
ACTION_SEND_MESSAGE = "send_message"
ACTION_DELETE = "delete"
ACTION_PURCHASE = "purchase"
ACTION_FINANCIAL_CHANGE = "financial_change"
ACTION_PERMISSION_CHANGE = "permission_change"
ACTION_EXTERNAL_PUBLISH = "external_publish"
ACTION_EXECUTE_CODE = "execute_code"

ACTION_TYPES = (
    ACTION_READ,
    ACTION_WRITE,
    ACTION_SEND_MESSAGE,
    ACTION_DELETE,
    ACTION_PURCHASE,
    ACTION_FINANCIAL_CHANGE,
    ACTION_PERMISSION_CHANGE,
    ACTION_EXTERNAL_PUBLISH,
    ACTION_EXECUTE_CODE,
)

SIDE_EFFECT_TYPES = frozenset(
    {
        ACTION_WRITE,
        ACTION_SEND_MESSAGE,
        ACTION_DELETE,
        ACTION_PURCHASE,
        ACTION_FINANCIAL_CHANGE,
        ACTION_PERMISSION_CHANGE,
        ACTION_EXTERNAL_PUBLISH,
        ACTION_EXECUTE_CODE,
    }
)

PROTECTED_IDEMPOTENCY_TYPES = SIDE_EFFECT_TYPES

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"
RISK_CLASSES = (RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL)

DECISION_ALLOW = "allow"
DECISION_REVIEW_AFTER = "review_after"
DECISION_REQUIRE_APPROVAL = "require_approval"
DECISION_DENY = "deny"
DECISIONS = (
    DECISION_ALLOW,
    DECISION_REVIEW_AFTER,
    DECISION_REQUIRE_APPROVAL,
    DECISION_DENY,
)

LEVEL_ADVISOR = "advisor"
LEVEL_ANALYST = "analyst"
LEVEL_EXECUTOR_CONFIRMED = "executor_confirmed"
LEVEL_EXECUTOR_BOUNDED = "executor_bounded"
AUTONOMY_LEVELS = (
    LEVEL_ADVISOR,
    LEVEL_ANALYST,
    LEVEL_EXECUTOR_CONFIRMED,
    LEVEL_EXECUTOR_BOUNDED,
)
DEFAULT_AUTONOMY_LEVEL = LEVEL_ANALYST

APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_EXPIRED = "expired"
APPROVAL_CANCELLED = "cancelled"
APPROVAL_STATUSES = (
    APPROVAL_PENDING,
    APPROVAL_APPROVED,
    APPROVAL_REJECTED,
    APPROVAL_EXPIRED,
    APPROVAL_CANCELLED,
)

IDEMPOTENCY_RESERVED = "reserved"
IDEMPOTENCY_STARTED = "started"
IDEMPOTENCY_COMPLETED = "completed"
IDEMPOTENCY_FAILED = "failed"
IDEMPOTENCY_STATES = (
    IDEMPOTENCY_RESERVED,
    IDEMPOTENCY_STARTED,
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_FAILED,
)
IDEMPOTENCY_ACTIVE = frozenset({IDEMPOTENCY_RESERVED, IDEMPOTENCY_STARTED})

FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "prompt",
        "authorization",
        "api_key",
        "cookie",
        "cookies",
        "password",
        "secret",
        "token",
        "signature",
        "encryption_key",
        "panda_encryption_key",
        "panda_capability_signing_key",
        "raw_body",
        "raw_provider",
        "bearer",
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_metadata(metadata: Mapping | None) -> dict:
    cleaned = {}
    for key, value in dict(metadata or {}).items():
        lowered = str(key).lower()
        if lowered in FORBIDDEN_METADATA_KEYS:
            continue
        if isinstance(value, str):
            cleaned[str(key)] = redact(value)
        elif isinstance(value, Mapping):
            cleaned[str(key)] = sanitize_metadata(value)
        else:
            cleaned[str(key)] = value
    return cleaned


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value))


@dataclass(frozen=True)
class ProposedAction:
    action_id: str
    workflow_id: str
    task_id: str
    action_type: str
    tool_id: str
    operation: str
    resource: str
    risk_class: str
    requested_capabilities: tuple[str, ...]
    tool_trust_level: str
    idempotency_key: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "requested_capabilities", tuple(self.requested_capabilities))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class AutonomyDecision:
    decision_id: str
    action_id: str
    decision: str
    risk_class: str
    reason_code: str
    required_approval: bool
    capabilities_checked: tuple[str, ...]
    idempotency_required: bool
    idempotency_satisfied: bool
    tool_trust_level: str
    timestamp: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.decision not in DECISIONS:
            raise ValueError(f"Invalid decision: {self.decision!r}")
        object.__setattr__(self, "capabilities_checked", tuple(self.capabilities_checked))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    workflow_id: str
    task_id: str
    action_id: str
    decision_id: str
    status: str
    approved_by: str
    created_at: datetime
    resolved_at: datetime | None = None
    reason_code: str | None = None

    def __post_init__(self):
        if self.status not in APPROVAL_STATUSES:
            raise ValueError(f"Invalid approval status: {self.status!r}")


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    action_id: str
    state: str
    created_at: datetime
    updated_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in IDEMPOTENCY_STATES:
            raise ValueError(f"Invalid idempotency state: {self.state!r}")
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class ToolDescriptor:
    tool_id: str
    source: str
    trust_level: str
    capabilities_required: tuple[str, ...]
    side_effects: bool
    reversible: bool
    network_access: bool
