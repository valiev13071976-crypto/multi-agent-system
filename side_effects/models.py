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
STATUS_STARTED = "started"
EXECUTION_STATUSES = (
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_UNKNOWN,
    STATUS_DENIED,
    STATUS_STARTED,
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

EVENT_ADAPTER_CONFIGURED = "adapter_configured"
EVENT_ADAPTER_DISABLED = "adapter_disabled"
EVENT_ADAPTER_DRY_RUN = "adapter_dry_run"
EVENT_ADAPTER_READY = "adapter_ready"
EVENT_ADAPTER_BLOCKED = "adapter_blocked"
EVENT_KILL_SWITCH_BLOCKED = "kill_switch_blocked"
EVENT_READINESS_PROBE_STARTED = "readiness_probe_started"
EVENT_READINESS_PROBE_PASSED = "readiness_probe_passed"
EVENT_READINESS_PROBE_FAILED = "readiness_probe_failed"
EVENT_DRY_RUN_REQUESTED = "dry_run_requested"
EVENT_DRY_RUN_COMPLETED = "dry_run_completed"
EVENT_REAL_EXECUTION_AUTHORIZED = "real_execution_authorized"

EVENT_RECONCILIATION_CREATED = "reconciliation_created"
EVENT_RECONCILIATION_STARTED = "reconciliation_started"
EVENT_RECONCILIATION_LOOKUP_SUCCEEDED = "reconciliation_lookup_succeeded"
EVENT_RECONCILIATION_LOOKUP_FAILED = "reconciliation_lookup_failed"
EVENT_RECONCILIATION_TIMEOUT = "reconciliation_timeout"
EVENT_RECONCILIATION_CONFIRMED_SUCCESS = "reconciliation_confirmed_success"
EVENT_RECONCILIATION_CONFIRMED_FAILURE = "reconciliation_confirmed_failure"
EVENT_RECONCILIATION_STILL_UNCERTAIN = "reconciliation_still_uncertain"
EVENT_RECONCILIATION_CONFLICT = "reconciliation_conflict"
EVENT_MANUAL_REVIEW_REQUIRED = "manual_review_required"
EVENT_MANUAL_RESOLUTION_SUCCESS = "manual_resolution_success"
EVENT_MANUAL_RESOLUTION_FAILURE = "manual_resolution_failure"
EVENT_RECOVERY_RETRY_ELIGIBLE = "recovery_retry_eligible"
EVENT_RECOVERY_RETRY_DENIED = "recovery_retry_denied"

RECON_PENDING = "pending"
RECON_CHECKING = "checking"
RECON_CONFIRMED_SUCCEEDED = "confirmed_succeeded"
RECON_CONFIRMED_FAILED = "confirmed_failed"
RECON_STILL_UNCERTAIN = "still_uncertain"
RECON_MANUAL_REVIEW = "manual_review"
RECON_RESOLVED = "resolved"
RECON_CANCELLED = "cancelled"
RECONCILIATION_STATUSES = (
    RECON_PENDING,
    RECON_CHECKING,
    RECON_CONFIRMED_SUCCEEDED,
    RECON_CONFIRMED_FAILED,
    RECON_STILL_UNCERTAIN,
    RECON_MANUAL_REVIEW,
    RECON_RESOLVED,
    RECON_CANCELLED,
)
RECONCILIATION_TERMINAL = frozenset(
    {
        RECON_CONFIRMED_SUCCEEDED,
        RECON_CONFIRMED_FAILED,
        RECON_MANUAL_REVIEW,
        RECON_RESOLVED,
        RECON_CANCELLED,
    }
)
RECONCILIATION_ACTIVE = frozenset(
    {RECON_PENDING, RECON_CHECKING, RECON_STILL_UNCERTAIN}
)

DECISION_NO_ACTION = "no_action"
DECISION_MARK_COMPLETED = "mark_completed"
DECISION_MARK_FAILED = "mark_failed"
DECISION_RETRY_ALLOWED = "retry_allowed"
DECISION_REAUTHORIZATION_REQUIRED = "reauthorization_required"
DECISION_MANUAL_REVIEW_REQUIRED = "manual_review_required"
DECISION_ROLLBACK_CANDIDATE = "rollback_candidate"
DECISION_DENY_RETRY = "deny_retry"
RECOVERY_DECISIONS = (
    DECISION_NO_ACTION,
    DECISION_MARK_COMPLETED,
    DECISION_MARK_FAILED,
    DECISION_RETRY_ALLOWED,
    DECISION_REAUTHORIZATION_REQUIRED,
    DECISION_MANUAL_REVIEW_REQUIRED,
    DECISION_ROLLBACK_CANDIDATE,
    DECISION_DENY_RETRY,
)

ADAPTER_RECON_SUCCEEDED = "succeeded"
ADAPTER_RECON_FAILED = "failed"
ADAPTER_RECON_NOT_FOUND = "not_found"
ADAPTER_RECON_UNKNOWN = "unknown"
ADAPTER_RECON_STATUSES = (
    ADAPTER_RECON_SUCCEEDED,
    ADAPTER_RECON_FAILED,
    ADAPTER_RECON_NOT_FOUND,
    ADAPTER_RECON_UNKNOWN,
)

WORKFLOW_RESOLUTION_EXTERNAL_CONFIRMED = "external_effect_confirmed"
WORKFLOW_RESOLUTION_REOPEN_REQUIRED = "workflow_reopen_required"
WORKFLOW_RESOLUTION_MANUAL_FOLLOWUP = "manual_followup_required"

DEFAULT_STARTED_STALE_AFTER_SECONDS = 300
DEFAULT_RECONCILIATION_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RECONCILIATION_ATTEMPTS = 3
DEFAULT_RECONCILIATION_BACKOFF_SECONDS = 5.0

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
    supports_reconciliation: bool = False
    reconciliation_authoritative: bool = False
    not_found_is_authoritative_failure: bool = False
    idempotency_mode: str = ""

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
    parent_execution_id: str | None = None
    reconciliation_id: str | None = None
    recovery_attempt: int = 0
    resource_ref: str | None = None
    reversible: bool = False
    version: int = 1
    permit_id: str | None = None
    approval_id: str | None = None
    # P1-SE-TENANT: first-class tenant ownership (empty = legacy unresolved only).
    tenant_id: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", str(self.tenant_id or ""))
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
    tenant_id: str | None = None

    def stamp(self) -> datetime:
        return self.now or utc_now()


@dataclass(frozen=True)
class AdapterReconciliationResult:
    status: str
    external_reference: str | None = None
    reversible: bool | None = None
    rollback_reference: str | None = None
    evidence_reference: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in ADAPTER_RECON_STATUSES:
            raise ValueError("invalid_adapter_reconciliation_status")
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class ReconciliationRecord:
    reconciliation_id: str
    execution_id: str
    workflow_id: str
    task_id: str
    action_id: str
    tool_id: str
    operation: str
    idempotency_key_hash: str
    status: str
    decision: str
    attempt: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    external_reference: str | None = None
    reason_code: str = "pending"
    version: int = 1
    resolver_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in RECONCILIATION_STATUSES:
            raise ValueError("invalid_reconciliation_status")
        if self.decision not in RECOVERY_DECISIONS:
            raise ValueError("invalid_recovery_decision")
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class ReconciliationResult:
    reconciliation_id: str
    execution_id: str
    status: str
    decision: str
    outcome: str | None
    external_reference: str | None
    retry_eligible: bool
    reauthorization_required: bool
    rollback_candidate: bool
    manual_review_required: bool
    reason_code: str
    checked_at: datetime
    workflow_resolution: str | None = None
    recovery_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class RecoveryLineage:
    recovery_id: str
    original_execution_id: str
    recovery_attempt: int
    parent_execution_id: str | None = None
    reconciliation_id: str | None = None


@dataclass(frozen=True)
class RecoveryWorkflowReference:
    original_workflow_id: str
    recovery_workflow_id: str | None
    reason: str


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
        supports_reconciliation=True,
        reconciliation_authoritative=True,
        not_found_is_authoritative_failure=False,
    )
