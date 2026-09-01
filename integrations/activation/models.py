"""Real Integration Activation contracts — provider vs connection, environments, lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id, require_tenant_id

PLATFORM_SCHEMA_VERSION = "1.0.0"

# Environments — mandatory distinction
ENV_FIXTURE = "FIXTURE"
ENV_SANDBOX = "SANDBOX"
ENV_LIVE = "LIVE"
ENVIRONMENTS = frozenset({ENV_FIXTURE, ENV_SANDBOX, ENV_LIVE})

# Connection lifecycle
STATUS_UNCONFIGURED = "UNCONFIGURED"
STATUS_CONFIGURED = "CONFIGURED"
STATUS_VERIFYING = "VERIFYING"
STATUS_ACTIVE = "ACTIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_FAILED = "FAILED"
STATUS_REVOKED = "REVOKED"
STATUS_DISABLED = "DISABLED"

ACTIVE_ELIGIBLE = frozenset({STATUS_ACTIVE, STATUS_DEGRADED})

# Auth types
AUTH_API_KEY = "API_KEY"
AUTH_BEARER = "BEARER"
AUTH_BASIC = "BASIC"
AUTH_OAUTH2 = "OAUTH2"
AUTH_SERVICE_ACCOUNT = "SERVICE_ACCOUNT"
AUTH_CUSTOM = "CUSTOM"

# Op classes
OP_READ = "READ"
OP_WRITE = "WRITE"
OP_ADMIN = "ADMIN"
OP_WEBHOOK = "WEBHOOK"
OP_TRIGGER = "TRIGGER"

# Health
HEALTH_HEALTHY = "HEALTHY"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_UNHEALTHY = "UNHEALTHY"
HEALTH_UNKNOWN = "UNKNOWN"

# Composio user connection
COMPOSIO_PROVIDER_CONFIGURED = "PROVIDER_CONFIGURED"
COMPOSIO_USER_REQUIRED = "USER_CONNECTION_REQUIRED"
COMPOSIO_USER_PENDING = "USER_CONNECTION_PENDING"
COMPOSIO_USER_ACTIVE = "USER_CONNECTION_ACTIVE"
COMPOSIO_USER_FAILED = "USER_CONNECTION_FAILED"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


@dataclass(frozen=True)
class IntegrationProvider:
    provider_id: str
    provider_type: str
    display_name: str
    capabilities: tuple[str, ...]
    auth_types: tuple[str, ...]
    supported_environments: tuple[str, ...] = (ENV_FIXTURE, ENV_SANDBOX, ENV_LIVE)
    adapter_id: str = ""
    read_capabilities: tuple[str, ...] = ()
    write_capabilities: tuple[str, ...] = ()
    supports_webhooks: bool = False
    supports_triggers: bool = False
    write_default_deny: bool = False
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "auth_types", tuple(self.auth_types))
        object.__setattr__(self, "supported_environments", tuple(self.supported_environments))
        object.__setattr__(self, "read_capabilities", tuple(self.read_capabilities))
        object.__setattr__(self, "write_capabilities", tuple(self.write_capabilities))
        object.__setattr__(self, "adapter_id", self.adapter_id or self.provider_id)


@dataclass(frozen=True)
class IntegrationCredentialRef:
    """Opaque secret pointer — never plaintext."""

    secret_ref: str
    tenant_id: str
    backend: str = "memory"
    version: int = 1

    def __post_init__(self):
        if not self.secret_ref or self.secret_ref.startswith("plain:"):
            raise ValueError("invalid_secret_ref")
        if any(x in self.secret_ref.lower() for x in ("token=", "password=", "api_key=")):
            raise ValueError("plaintext_looking_secret_ref")
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class IntegrationCapabilityBinding:
    capability: str
    operation_class: str  # READ / WRITE
    provider_id: str
    enabled: bool = True


@dataclass(frozen=True)
class IntegrationConnection:
    connection_id: str
    tenant_id: str
    provider_id: str
    environment: str
    credential_ref: str
    status: str = STATUS_UNCONFIGURED
    owner_id: str = ""
    external_account_ref: str = ""
    priority: int = 100
    read_capabilities: tuple[str, ...] = ()
    write_capabilities: tuple[str, ...] = ()
    auth_type: str = AUTH_API_KEY
    last_verified_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema_version: str = PLATFORM_SCHEMA_VERSION
    created_at: datetime = field(default_factory=_utc)
    updated_at: datetime = field(default_factory=_utc)
    # Composio-specific user connection state (empty for native)
    user_connection_status: str = ""

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        if self.environment not in ENVIRONMENTS:
            raise ValueError("invalid_environment")
        if self.credential_ref and (
            "=" in self.credential_ref
            or self.credential_ref.startswith("sk-")
            or len(self.credential_ref) > 200
            and " " in self.credential_ref
        ):
            # Reject obvious plaintext blobs in credential_ref field
            if any(k in self.credential_ref.lower() for k in ("bearer ", "api_key", "password", "secret=")):
                raise ValueError("plaintext_credential_rejected")
        object.__setattr__(self, "read_capabilities", tuple(self.read_capabilities))
        object.__setattr__(self, "write_capabilities", tuple(self.write_capabilities))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class IntegrationVerification:
    connection_id: str
    tenant_id: str
    authentication_valid: bool
    required_capabilities_available: bool
    environment: str
    verified_at: datetime
    evidence_id: str
    provider_identity: str = ""
    failure_category: str = ""
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "details", _meta(self.details))


@dataclass(frozen=True)
class IntegrationActivation:
    connection_id: str
    tenant_id: str
    status: str
    environment: str
    activated_at: datetime | None
    verification_evidence_id: str = ""


@dataclass(frozen=True)
class IntegrationHealthView:
    provider_id: str
    connection_id: str
    environment: str
    status: str
    timestamp: datetime
    latency_ms: float | None = None
    error_category: str = ""
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "details", _meta(self.details))


@dataclass(frozen=True)
class IntegrationEvidence:
    evidence_id: str
    tenant_id: str
    connection_id: str
    provider_id: str
    event_type: str
    environment: str
    capability: str = ""
    operation_class: str = ""
    status: str = ""
    correlation_id: str = ""
    workflow_id: str = ""
    latency_ms: float | None = None
    error_category: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class ResolvedConnection:
    connection: IntegrationConnection
    provider: IntegrationProvider
    capability: str
    operation_class: str
    environment: str
