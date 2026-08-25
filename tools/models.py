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
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.trust_level not in TOOL_TRUST_LEVELS:
            raise ValueError(f"Invalid trust_level: {self.trust_level!r}")
        if not str(self.version or "").strip():
            raise ValueError("tool_version_required")
        if not str(self.tool_id or "").strip():
            raise ValueError("tool_id_required")
        if not self.operations:
            raise ValueError("tool_operations_required")
        object.__setattr__(self, "capabilities_required", tuple(self.capabilities_required))
        object.__setattr__(self, "action_types_supported", tuple(self.action_types_supported))
        object.__setattr__(self, "operations", tuple(self.operations))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        if self.read_only and self.trust_level in WRITE_TRUST_LEVELS:
            raise ValueError("read_only_trust_mismatch")
        if (
            not self.read_only
            and self.trust_level == TOOL_TRUST_READ_ONLY_EXTERNAL
        ):
            raise ValueError("write_tool_cannot_be_read_only_external")


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
    metadata: Mapping[str, object] = field(default_factory=dict)

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
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"Invalid tool status: {self.status!r}")
        object.__setattr__(self, "data", _meta(self.data))
        object.__setattr__(self, "metadata", _meta(self.metadata))


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
