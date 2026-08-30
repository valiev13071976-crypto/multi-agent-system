from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping


TRUST_UNKNOWN = "unknown"
TRUST_LOW = "low"
TRUST_MEDIUM = "medium"
TRUST_HIGH = "high"
ALLOWED_TRUST = (TRUST_UNKNOWN, TRUST_LOW, TRUST_MEDIUM, TRUST_HIGH)

EVIDENCE_SUPPORTED = "supported"
EVIDENCE_CONTRADICTED = "contradicted"
EVIDENCE_INSUFFICIENT = "insufficient"
EVIDENCE_UNKNOWN = "unknown"
ALLOWED_EVIDENCE_STATUS = (
    EVIDENCE_SUPPORTED,
    EVIDENCE_CONTRADICTED,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_UNKNOWN,
)

TOOL_TRUST_INTERNAL_SAFE = "INTERNAL_SAFE"
TOOL_TRUST_READ_ONLY_EXTERNAL = "READ_ONLY_EXTERNAL"
TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE = "WRITE_EXTERNAL_REVERSIBLE"
TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE = "WRITE_EXTERNAL_IRREVERSIBLE"
TOOL_TRUST_PRIVILEGED = "PRIVILEGED"
TOOL_TRUST_LEVELS = (
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    TOOL_TRUST_PRIVILEGED,
)

# Platform trust aliases (map onto TOOL_TRUST_*)
TRUSTED_INTERNAL = "TRUSTED_INTERNAL"
TRUSTED_EXTERNAL = "TRUSTED_EXTERNAL"
RESTRICTED = "RESTRICTED"
UNTRUSTED = "UNTRUSTED"
PLATFORM_TRUST_ALIASES = (
    TRUSTED_INTERNAL,
    TRUSTED_EXTERNAL,
    RESTRICTED,
    UNTRUSTED,
)
PLATFORM_TRUST_TO_TOOL_TRUST = {
    TRUSTED_INTERNAL: TOOL_TRUST_INTERNAL_SAFE,
    TRUSTED_EXTERNAL: TOOL_TRUST_READ_ONLY_EXTERNAL,
    RESTRICTED: TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    UNTRUSTED: TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
}

# Per-operation classification
OP_READ = "read"
OP_WRITE = "write"
OP_DESTRUCTIVE = "destructive"
OP_EXTERNAL_COMMUNICATION = "external_communication"
OP_PRIVILEGED = "privileged"
OPERATION_CLASSES = (
    OP_READ,
    OP_WRITE,
    OP_DESTRUCTIVE,
    OP_EXTERNAL_COMMUNICATION,
    OP_PRIVILEGED,
)

# Version lifecycle
VERSION_ACTIVE = "active"
VERSION_DEPRECATED = "deprecated"
VERSION_DISABLED = "disabled"
VERSION_STATUSES = (VERSION_ACTIVE, VERSION_DEPRECATED, VERSION_DISABLED)

# Workload / sensitivity / cost / approval governance tokens
WORKLOAD_HINT_INTERACTIVE = "interactive"
WORKLOAD_HINT_NORMAL = "normal"
WORKLOAD_HINT_BATCH = "batch"
WORKLOAD_HINT_BACKGROUND = "background"
WORKLOAD_HINTS = (
    WORKLOAD_HINT_INTERACTIVE,
    WORKLOAD_HINT_NORMAL,
    WORKLOAD_HINT_BATCH,
    WORKLOAD_HINT_BACKGROUND,
)

DATA_SENSITIVITY_PUBLIC = "public"
DATA_SENSITIVITY_INTERNAL = "internal"
DATA_SENSITIVITY_CONFIDENTIAL = "confidential"
DATA_SENSITIVITY_RESTRICTED = "restricted"
DATA_SENSITIVITY_LEVELS = (
    DATA_SENSITIVITY_PUBLIC,
    DATA_SENSITIVITY_INTERNAL,
    DATA_SENSITIVITY_CONFIDENTIAL,
    DATA_SENSITIVITY_RESTRICTED,
)

COST_CLASS_FREE = "free"
COST_CLASS_LOW = "low"
COST_CLASS_MEDIUM = "medium"
COST_CLASS_HIGH = "high"
COST_CLASSES = (COST_CLASS_FREE, COST_CLASS_LOW, COST_CLASS_MEDIUM, COST_CLASS_HIGH)

APPROVAL_POLICY_NONE = "none"
APPROVAL_POLICY_REQUIRED = "required"
APPROVAL_POLICY_POLICY = "policy"
APPROVAL_POLICIES = (
    APPROVAL_POLICY_NONE,
    APPROVAL_POLICY_REQUIRED,
    APPROVAL_POLICY_POLICY,
)

TENANT_SCOPE_REQUIRED = "required"
TENANT_SCOPE_OPTIONAL = "optional"
TENANT_SCOPE_POLICIES = (TENANT_SCOPE_REQUIRED, TENANT_SCOPE_OPTIONAL)

AUTH_REQUIREMENT_NONE = "none"
AUTH_REQUIREMENT_SECRET_REF = "secret_ref"
AUTH_REQUIREMENTS = (AUTH_REQUIREMENT_NONE, AUTH_REQUIREMENT_SECRET_REF)


def resolve_platform_trust(platform_trust: str | None, trust_level: str | None = None) -> str:
    """Map platform trust alias → canonical TOOL_TRUST_* (or pass-through)."""
    alias = str(platform_trust or "").strip()
    if alias in PLATFORM_TRUST_TO_TOOL_TRUST:
        return PLATFORM_TRUST_TO_TOOL_TRUST[alias]
    level = str(trust_level or "").strip()
    if level in TOOL_TRUST_LEVELS:
        return level
    if alias in TOOL_TRUST_LEVELS:
        return alias
    return TOOL_TRUST_READ_ONLY_EXTERNAL


def operation_class_for(
    descriptor: "ToolDescriptor",
    operation: str,
    *,
    default: str | None = None,
) -> str:
    """Resolve per-op class from descriptor.operation_class map."""
    op = str(operation or "").strip()
    mapping = dict(descriptor.operation_class or {})
    if op and op in mapping:
        value = str(mapping[op])
        if value in OPERATION_CLASSES:
            return value
    if default and default in OPERATION_CLASSES:
        return default
    if descriptor.read_only:
        return OP_READ
    return OP_WRITE

# Canonical side-effect levels (maps to trust for policy)
SIDE_EFFECT_NONE = "none"
SIDE_EFFECT_READ = "read"
SIDE_EFFECT_WRITE = "write"
SIDE_EFFECT_CRITICAL = "critical"
SIDE_EFFECT_LEVELS = (
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_READ,
    SIDE_EFFECT_WRITE,
    SIDE_EFFECT_CRITICAL,
)

# Retry policy tokens — execution defers to Workflow/TaskQueue when transient
RETRY_NONE = "none"
RETRY_TRANSIENT = "transient"
RETRY_WORKFLOW = "workflow"
RETRY_POLICIES = (RETRY_NONE, RETRY_TRANSIENT, RETRY_WORKFLOW)

# Adapter health states (separate from ModelHealth)
ADAPTER_HEALTHY = "healthy"
ADAPTER_DEGRADED = "degraded"
ADAPTER_UNAVAILABLE = "unavailable"
ADAPTER_UNKNOWN = "unknown"
ADAPTER_HEALTH_STATES = (
    ADAPTER_HEALTHY,
    ADAPTER_DEGRADED,
    ADAPTER_UNAVAILABLE,
    ADAPTER_UNKNOWN,
)

WRITE_TRUST_LEVELS = frozenset(
    {
        TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
        TOOL_TRUST_PRIVILEGED,
    }
)

MAX_FACT_CLAIMS = 3
MAX_SEARCH_RESULTS_PER_CLAIM = 5
MAX_TOTAL_SEARCH_RESULTS = 10
DEFAULT_SEARCH_TIMEOUT_SECONDS = 5.0

MAX_TOOL_ARGUMENT_BYTES = 16_384
MAX_TOOL_ARGUMENT_DEPTH = 4
MAX_TOOL_ARGUMENT_LIST_LEN = 64
MAX_TOOL_ARGUMENT_STRING_LEN = 4_096
MAX_TOOL_RESULT_DATA_BYTES = 65_536

SEARCH_TOOL_ID = "search"
SEARCH_OPERATION = "search"

TOOL_STATUS_SUCCEEDED = "succeeded"
TOOL_STATUS_FAILED = "failed"
TOOL_STATUS_DENIED = "denied"
TOOL_STATUS_APPROVAL_REQUIRED = "approval_required"
TOOL_STATUS_UNCERTAIN = "uncertain"
TOOL_STATUSES = (
    TOOL_STATUS_SUCCEEDED,
    TOOL_STATUS_FAILED,
    TOOL_STATUS_DENIED,
    TOOL_STATUS_APPROVAL_REQUIRED,
    TOOL_STATUS_UNCERTAIN,
)

FORBIDDEN_BYPASS_KEYS = frozenset(
    {
        "pre_authorized",
        "pre-authorized",
        "skip_gate",
        "force",
        "admin_override",
        "bypass_policy",
        "bypass_hitl",
        "skip_permit",
    }
)

FORBIDDEN_DYNAMIC_KEYS = frozenset(
    {
        "module",
        "module_path",
        "class_name",
        "python_code",
        "code",
        "shell",
        "shell_command",
        "command",
        "base_url",
        "endpoint_url",
        "import_path",
        "headers",
        "auth",
        "authorization",
        "timeout",
        "method",
        "raw_url",
    }
)


def _meta(value) -> Mapping[str, object]:
    from autonomy.models import sanitize_metadata

    return MappingProxyType(sanitize_metadata(value))


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source_domain: str
    published_at: datetime | None
    retrieved_at: datetime
    trust_level: str

    def __post_init__(self):
        if self.trust_level not in ALLOWED_TRUST:
            raise ValueError(f"Invalid trust_level: {self.trust_level!r}")
        object.__setattr__(self, "title", str(self.title or ""))
        object.__setattr__(self, "url", str(self.url or ""))
        object.__setattr__(self, "snippet", str(self.snippet or ""))
        object.__setattr__(self, "source_domain", str(self.source_domain or ""))


@dataclass(frozen=True)
class EvidenceResult:
    claim: str
    status: str
    supporting_sources: tuple[str, ...]
    contradicting_sources: tuple[str, ...]
    confidence: float
    reason: str

    def __post_init__(self):
        if self.status not in ALLOWED_EVIDENCE_STATUS:
            raise ValueError(f"Invalid evidence status: {self.status!r}")
        score = float(self.confidence)
        if score < 0.0:
            score = 0.0
        if score > 1.0:
            score = 1.0
        object.__setattr__(self, "confidence", score)
        object.__setattr__(self, "supporting_sources", tuple(self.supporting_sources))
        object.__setattr__(self, "contradicting_sources", tuple(self.contradicting_sources))
        object.__setattr__(self, "claim", str(self.claim))
        object.__setattr__(self, "reason", str(self.reason))


@dataclass(frozen=True)
class ToolUsageRecord:
    tool_id: str
    task_id: str
    operation: str
    timestamp: datetime
    success: bool
    latency_ms: int
    metadata: Mapping[str, object] = None

    def __post_init__(self):
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata or {})),
        )


@dataclass(frozen=True)
class ToolDescriptor:
    """Canonical registry descriptor for ToolGateway (read and write tools)."""

    tool_id: str
    name: str
    description: str
    version: str
    trust_level: str
    capabilities_required: tuple[str, ...]
    action_types_supported: tuple[str, ...]
    operations: tuple[str, ...]
    read_only: bool
    reversible: bool
    idempotency_required: bool
    timeout_seconds: float
    enabled: bool = True
    network_access: bool = False
    resource_prefix: str = ""
    schema_hash: str = ""
    category: str = ""
    adapter_id: str = ""
    side_effect_level: str = SIDE_EFFECT_NONE
    retry_policy: str = RETRY_NONE
    input_schema_ref: str = ""
    output_schema_ref: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    # Governance extensions (optional; defaults preserve BC)
    operation_class: Mapping[str, str] = field(default_factory=dict)
    platform_trust: str = ""
    workload_class_hint: str = ""
    data_sensitivity: str = DATA_SENSITIVITY_INTERNAL
    cost_class: str = ""
    cost_unknown: bool = True
    approval_policy: str = APPROVAL_POLICY_NONE
    tenant_scope_policy: str = TENANT_SCOPE_OPTIONAL
    auth_requirement: str = AUTH_REQUIREMENT_NONE
    idempotency_capability: bool = False
    version_status: str = VERSION_ACTIVE

    def __post_init__(self):
        alias = str(self.platform_trust or "").strip()
        raw_trust = str(self.trust_level or "").strip()
        if alias:
            if alias not in PLATFORM_TRUST_ALIASES and alias not in TOOL_TRUST_LEVELS:
                raise ValueError(f"Invalid platform_trust: {self.platform_trust!r}")
            resolved_trust = resolve_platform_trust(alias, raw_trust)
        else:
            if raw_trust not in TOOL_TRUST_LEVELS:
                raise ValueError(f"Invalid trust_level: {self.trust_level!r}")
            resolved_trust = raw_trust
        if resolved_trust not in TOOL_TRUST_LEVELS:
            raise ValueError(f"Invalid trust_level: {self.trust_level!r}")
        object.__setattr__(self, "trust_level", resolved_trust)
        if self.side_effect_level not in SIDE_EFFECT_LEVELS:
            raise ValueError(f"Invalid side_effect_level: {self.side_effect_level!r}")
        if self.retry_policy not in RETRY_POLICIES:
            raise ValueError(f"Invalid retry_policy: {self.retry_policy!r}")
        if self.version_status not in VERSION_STATUSES:
            raise ValueError(f"Invalid version_status: {self.version_status!r}")
        if self.data_sensitivity not in DATA_SENSITIVITY_LEVELS:
            raise ValueError(f"Invalid data_sensitivity: {self.data_sensitivity!r}")
        if self.approval_policy not in APPROVAL_POLICIES:
            raise ValueError(f"Invalid approval_policy: {self.approval_policy!r}")
        if self.tenant_scope_policy not in TENANT_SCOPE_POLICIES:
            raise ValueError(f"Invalid tenant_scope_policy: {self.tenant_scope_policy!r}")
        if self.auth_requirement not in AUTH_REQUIREMENTS:
            raise ValueError(f"Invalid auth_requirement: {self.auth_requirement!r}")
        if self.workload_class_hint and self.workload_class_hint not in WORKLOAD_HINTS:
            raise ValueError(f"Invalid workload_class_hint: {self.workload_class_hint!r}")
        if self.cost_class and self.cost_class not in COST_CLASSES:
            raise ValueError(f"Invalid cost_class: {self.cost_class!r}")
        if not str(self.version or "").strip():
            raise ValueError("tool_version_required")
        if not str(self.tool_id or "").strip():
            raise ValueError("tool_id_required")
        if not self.operations:
            raise ValueError("tool_operations_required")
        object.__setattr__(self, "capabilities_required", tuple(self.capabilities_required))
        object.__setattr__(self, "action_types_supported", tuple(self.action_types_supported))
        object.__setattr__(self, "operations", tuple(self.operations))
        if not self.adapter_id:
            object.__setattr__(self, "adapter_id", self.tool_id.split(".", 1)[0])
        object.__setattr__(self, "metadata", _meta(self.metadata))
        op_map = {
            str(k): str(v)
            for k, v in dict(self.operation_class or {}).items()
            if str(v) in OPERATION_CLASSES
        }
        object.__setattr__(self, "operation_class", MappingProxyType(op_map))
        # idempotency_capability mirrors required when not explicitly set usefully
        if self.idempotency_required and not self.idempotency_capability:
            object.__setattr__(self, "idempotency_capability", True)
        if self.read_only and self.trust_level in WRITE_TRUST_LEVELS:
            raise ValueError("read_only_trust_mismatch")
        if (
            not self.read_only
            and self.trust_level == TOOL_TRUST_READ_ONLY_EXTERNAL
        ):
            raise ValueError("write_tool_cannot_be_read_only_external")

    def operation_class_for(self, operation: str, *, default: str | None = None) -> str:
        return operation_class_for(self, operation, default=default)


@dataclass(frozen=True)
class ToolRequest:
    request_id: str
    workflow_id: str
    task_id: str
    tool_id: str
    operation: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    actor_id: str = ""
    requested_capabilities: tuple[str, ...] = ()
    idempotency_key: str | None = None
    dry_run: bool = False
    created_at: datetime | None = None
    correlation_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    step_id: str = ""
    capability_context: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    # Trusted context fields — filled from RunEnvelope/context, not user identity override
    tool_version: str = ""
    execution_id: str = ""
    data_scope_ref: str = ""
    capability_scope_ref: str = ""
    deadline: datetime | None = None
    envelope_ref: str = ""

    def __post_init__(self):
        object.__setattr__(self, "arguments", _meta(self.arguments))
        object.__setattr__(
            self, "requested_capabilities", tuple(self.requested_capabilities)
        )
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class ToolResult:
    request_id: str
    tool_id: str
    operation: str
    status: str
    success: bool
    data: Mapping[str, object] = field(default_factory=dict)
    error_code: str | None = None
    error_message_safe: str | None = None
    trust_level: str = TOOL_TRUST_READ_ONLY_EXTERNAL
    side_effect: bool = False
    execution_id: str | None = None
    approval_id: str | None = None
    permit_id: str | None = None
    external_reference: str | None = None
    duration_ms: int = 0
    adapter_id: str = ""
    provenance: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    retryable: bool = False
    reason_code: str = ""
    artifact_refs: tuple[str, ...] = ()
    usage: Mapping[str, object] = field(default_factory=dict)
    audit_ref: str = ""

    def __post_init__(self):
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"Invalid tool status: {self.status!r}")
        object.__setattr__(self, "data", _meta(self.data))
        object.__setattr__(self, "provenance", _meta(self.provenance))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs or ()))
        object.__setattr__(self, "usage", _meta(self.usage))


@dataclass(frozen=True)
class ToolExecutionContext:
    workflow_id: str = ""
    task_id: str = ""
    actor_id: str = ""
    capabilities: object | None = None
    correlation_id: str = ""
    deadline: datetime | None = None
    dry_run: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))
