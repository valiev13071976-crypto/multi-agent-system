import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata, utc_now
from tools.models import TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE


AUTHORIZATION_AUTONOMY_DECISION = "autonomy_decision"
AUTHORIZATION_EXECUTION_PERMIT = "execution_permit"
AUTHORIZATION_TYPES = (
    AUTHORIZATION_AUTONOMY_DECISION,
    AUTHORIZATION_EXECUTION_PERMIT,
)

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"
STATUS_DENIED = "denied"
EXECUTION_STATUSES = (
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_UNKNOWN,
    STATUS_DENIED,
)

OUTCOME_KNOWN_SUCCESS = "known_success"
OUTCOME_KNOWN_FAILURE = "known_failure"
OUTCOME_UNCERTAIN = "uncertain"
EXECUTION_OUTCOMES = (
    OUTCOME_KNOWN_SUCCESS,
    OUTCOME_KNOWN_FAILURE,
    OUTCOME_UNCERTAIN,
)

ROLLBACK_NONE = "none"
ROLLBACK_REQUESTED = "requested"
ROLLBACK_SUCCEEDED = "succeeded"
ROLLBACK_FAILED = "failed"
ROLLBACK_STATUSES = (
    ROLLBACK_NONE,
    ROLLBACK_REQUESTED,
    ROLLBACK_SUCCEEDED,
    ROLLBACK_FAILED,
)

EVENT_EXECUTION_REQUESTED = "execution_requested"
EVENT_EXECUTION_AUTHORIZED = "execution_authorized"
EVENT_EXECUTION_DENIED = "execution_denied"
EVENT_IDEMPOTENCY_RESERVED = "idempotency_reserved"
EVENT_PERMIT_CONSUMED = "permit_consumed"
EVENT_ADAPTER_STARTED = "adapter_started"
EVENT_ADAPTER_SUCCEEDED = "adapter_succeeded"
EVENT_ADAPTER_FAILED = "adapter_failed"
EVENT_EXECUTION_UNCERTAIN = "execution_uncertain"
EVENT_EXECUTION_COMPLETED = "execution_completed"
EVENT_ROLLBACK_REQUESTED = "rollback_requested"
EVENT_ROLLBACK_SUCCEEDED = "rollback_succeeded"
EVENT_ROLLBACK_FAILED = "rollback_failed"

TEST_TOOL_ID = "test.reversible_store"
TEST_OPERATION_SET_VALUE = "set_value"
TEST_RESOURCE_PREFIX = "test/"

# In-memory duplicate prevention only. Not a distributed exactly-once guarantee.
IDEMPOTENCY_SEMANTICS = "best-effort single-process duplicate prevention"

P6A_EXECUTABLE_ACTION_TYPES = frozenset({"write"})

DISABLED_ACTION_REASONS = {
    "purchase": "financial_execution_not_enabled",
    "financial_change": "financial_execution_not_enabled",
    "send_message": "customer_communication_execution_not_enabled",
    "external_publish": "customer_communication_execution_not_enabled",
    "permission_change": "permission_change_execution_not_enabled",
    "delete": "delete_execution_not_enabled",
    "execute_code": "code_execution_not_enabled",
}


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value))


def hash_idempotency_key(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def value_fingerprint(value) -> dict:
    raw = repr(value).encode("utf-8")
    return {
        "value_type": type(value).__name__,
        "value_length": len(raw),
        "value_hash": hashlib.sha256(raw).hexdigest(),
    }


@dataclass(frozen=True)
class SideEffectToolDescriptor:
    tool_id: str
    trust_level: str
    capabilities_required: tuple[str, ...]
    reversible: bool
    supports_idempotency: bool
    network_access: bool
    operations: tuple[str, ...]
    resource_prefix: str = TEST_RESOURCE_PREFIX

    def __post_init__(self):
        object.__setattr__(self, "capabilities_required", tuple(self.capabilities_required))
        object.__setattr__(self, "operations", tuple(self.operations))


@dataclass(frozen=True)
class AdapterExecutionResult:
    success: bool
    external_reference: str | None
    reversible: bool
    rollback_reference: str | None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class RollbackResult:
    success: bool
    rollback_reference: str | None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class SideEffectExecutionRequest:
    execution_id: str
    workflow_id: str
    task_id: str
    action_id: str
    tool_id: str
    operation: str
    resource: str
    action_fingerprint: str
    idempotency_key: str
    authorization_type: str
    authorization_id: str
    requested_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.authorization_type not in AUTHORIZATION_TYPES:
            raise ValueError("invalid_authorization_type")
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class SideEffectExecutionResult:
    execution_id: str
    workflow_id: str
    task_id: str
    action_id: str
    tool_id: str
    operation: str
    status: str
    started_at: datetime
    completed_at: datetime
    outcome: str
    external_reference: str | None = None
    reversible: bool = False
    rollback_reference: str | None = None
    error_code: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in EXECUTION_STATUSES:
            raise ValueError("invalid_execution_status")
        if self.outcome not in EXECUTION_OUTCOMES:
            raise ValueError("invalid_execution_outcome")
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class SideEffectExecutionRecord:
    execution_id: str
    action_id: str
    workflow_id: str
    task_id: str
    tool_id: str
    operation: str
    status: str
    authorization_type: str
    authorization_id: str
    idempotency_key_hash: str
    attempt: int
    started_at: datetime
    completed_at: datetime | None
    error_code: str | None = None
    external_reference: str | None = None
    rollback_status: str = ROLLBACK_NONE
    rollback_reference: str | None = None
    outcome: str = OUTCOME_KNOWN_FAILURE
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass
class SideEffectExecutionContext:
    """Ephemeral runtime context. Never persist; never checkpoint payloads."""

    payload: Mapping[str, object] = field(default_factory=dict)
    now: datetime | None = None
    capabilities: object | None = None
    token: object | None = None
    approval: object | None = None
    autonomy_level: str | None = None
    simulate_finalization_failure: bool = False
    timeout_seconds: float | None = None
    idempotency_key: str | None = None
    resource: str | None = None

    def stamp(self) -> datetime:
        return self.now or utc_now()


def default_test_descriptor(
    *,
    trust_level: str = TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    reversible: bool = True,
    operations: tuple[str, ...] = (TEST_OPERATION_SET_VALUE,),
) -> SideEffectToolDescriptor:
    from autonomy.capabilities import CAP_EXTERNAL_WRITE

    return SideEffectToolDescriptor(
        tool_id=TEST_TOOL_ID,
        trust_level=trust_level,
        capabilities_required=(CAP_EXTERNAL_WRITE,),
        reversible=reversible,
        supports_idempotency=True,
        network_access=False,
        operations=operations,
        resource_prefix=TEST_RESOURCE_PREFIX,
    )
