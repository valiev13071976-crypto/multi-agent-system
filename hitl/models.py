import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import ApprovalRecord, sanitize_metadata, utc_now
from tools.models import TOOL_TRUST_PRIVILEGED


APPROVAL_CLASS_STANDARD = "standard"
APPROVAL_CLASS_HIGH_RISK = "high_risk"
APPROVAL_CLASS_CRITICAL = "critical"
APPROVAL_CLASS_PRIVILEGED = "privileged"

APPROVAL_CLASSES = (
    APPROVAL_CLASS_STANDARD,
    APPROVAL_CLASS_HIGH_RISK,
    APPROVAL_CLASS_CRITICAL,
    APPROVAL_CLASS_PRIVILEGED,
)

APPROVAL_CLASS_RANK = {
    APPROVAL_CLASS_STANDARD: 0,
    APPROVAL_CLASS_HIGH_RISK: 1,
    APPROVAL_CLASS_CRITICAL: 2,
    APPROVAL_CLASS_PRIVILEGED: 3,
}

PERMIT_ISSUED = "issued"
PERMIT_CONSUMED = "consumed"
PERMIT_EXPIRED = "expired"
PERMIT_REVOKED = "revoked"
PERMIT_STATUSES = (
    PERMIT_ISSUED,
    PERMIT_CONSUMED,
    PERMIT_EXPIRED,
    PERMIT_REVOKED,
)

ROLE_STANDARD_APPROVER = "standard_approver"
ROLE_HIGH_RISK_APPROVER = "high_risk_approver"
ROLE_CRITICAL_APPROVER = "critical_approver"
ROLE_PRIVILEGED_APPROVER = "privileged_approver"

APPROVER_ROLE_RANK = {
    ROLE_STANDARD_APPROVER: 0,
    ROLE_HIGH_RISK_APPROVER: 1,
    ROLE_CRITICAL_APPROVER: 2,
    ROLE_PRIVILEGED_APPROVER: 3,
}

EVENT_APPROVAL_REQUESTED = "approval_requested"
EVENT_APPROVAL_APPROVED = "approval_approved"
EVENT_APPROVAL_REJECTED = "approval_rejected"
EVENT_APPROVAL_EXPIRED = "approval_expired"
EVENT_APPROVAL_CANCELLED = "approval_cancelled"
EVENT_REEVALUATION_PASSED = "reevaluation_passed"
EVENT_REEVALUATION_FAILED = "reevaluation_failed"
EVENT_PERMIT_ISSUED = "permit_issued"
EVENT_PERMIT_CONSUMED = "permit_consumed"
EVENT_PERMIT_EXPIRED = "permit_expired"
EVENT_PERMIT_REVOKED = "permit_revoked"

DEFAULT_APPROVAL_TTL_SECONDS = 3600
DEFAULT_PERMIT_TTL_SECONDS = 300

ApprovalRequest = ApprovalRecord


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value))


def action_fingerprint(action) -> str:
    payload = {
        "action_type": action.action_type,
        "tool_id": action.tool_id,
        "operation": action.operation,
        "resource": action.resource,
        "requested_capabilities": sorted(action.requested_capabilities),
        "risk_class": action.risk_class,
        "tool_trust_level": action.tool_trust_level,
        "reversible": bool(dict(action.metadata).get("reversible", False)),
        "idempotency_key": action.idempotency_key,
        "workflow_id": action.workflow_id,
        "task_id": action.task_id,
        "tenant_id": str(getattr(action, "tenant_id", "") or ""),
        "actor_ref": str(getattr(action, "actor_ref", "") or ""),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def approval_class_for(action) -> str:
    trust = action.tool_trust_level
    risk = action.risk_class
    kind = action.action_type
    if trust == TOOL_TRUST_PRIVILEGED or kind == "permission_change":
        return APPROVAL_CLASS_PRIVILEGED
    if risk == "critical" or kind in {"purchase", "financial_change"}:
        return APPROVAL_CLASS_CRITICAL
    if risk == "high" or kind in {"send_message", "external_publish", "delete"}:
        return APPROVAL_CLASS_HIGH_RISK
    return APPROVAL_CLASS_STANDARD


def class_sufficient(approved_class: str, required_class: str) -> bool:
    return APPROVAL_CLASS_RANK.get(approved_class, -1) >= APPROVAL_CLASS_RANK.get(
        required_class, 99
    )


@dataclass(frozen=True)
class ExecutionPermit:
    permit_id: str
    workflow_id: str
    task_id: str
    action_id: str
    approval_id: str
    decision_id: str
    action_fingerprint: str
    issued_at: datetime
    expires_at: datetime
    capabilities: tuple[str, ...]
    tool_id: str
    operation: str
    idempotency_key: str | None
    single_use: bool = True
    status: str = PERMIT_ISSUED
    consumed_at: datetime | None = None
    version: int = 1
    metadata: Mapping[str, object] = field(default_factory=dict)
    tenant_id: str = ""
    actor_ref: str = ""

    def __post_init__(self):
        if self.status not in PERMIT_STATUSES:
            raise ValueError(f"Invalid permit status: {self.status!r}")
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        object.__setattr__(self, "tenant_id", str(self.tenant_id or ""))
        object.__setattr__(self, "actor_ref", str(self.actor_ref or ""))

    def public_view(self) -> dict:
        return {
            "permit_id": self.permit_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "action_id": self.action_id,
            "approval_id": self.approval_id,
            "status": self.status,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class HITLAuditEvent:
    event_id: str
    workflow_id: str
    task_id: str
    action_id: str
    event_type: str
    timestamp: datetime
    approval_id: str | None = None
    permit_id: str | None = None
    actor_id: str | None = None
    reason_code: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))
        object.__setattr__(self, "timestamp", self.timestamp or utc_now())
