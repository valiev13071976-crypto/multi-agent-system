"""Canonical integration contracts — immutable, no plaintext secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


# Credential lifecycle
CREDENTIAL_ACTIVE = "active"
CREDENTIAL_EXPIRING = "expiring"
CREDENTIAL_EXPIRED = "expired"
CREDENTIAL_ROTATING = "rotating"
CREDENTIAL_REVOKED = "revoked"
CREDENTIAL_INVALID = "invalid"
CREDENTIAL_STATES = frozenset(
    {
        CREDENTIAL_ACTIVE,
        CREDENTIAL_EXPIRING,
        CREDENTIAL_EXPIRED,
        CREDENTIAL_ROTATING,
        CREDENTIAL_REVOKED,
        CREDENTIAL_INVALID,
    }
)

# Health
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNAVAILABLE = "unavailable"
HEALTH_AUTH_FAILED = "auth_failed"
HEALTH_UNKNOWN = "unknown"
HEALTH_STATES = frozenset(
    {
        HEALTH_HEALTHY,
        HEALTH_DEGRADED,
        HEALTH_UNAVAILABLE,
        HEALTH_AUTH_FAILED,
        HEALTH_UNKNOWN,
    }
)

# Auth strategies
AUTH_API_KEY = "api_key"
AUTH_BEARER = "bearer"
AUTH_OAUTH2 = "oauth2"
AUTH_SERVICE_ACCOUNT = "service_account"
AUTH_BASIC = "basic"
AUTH_SIGNED = "signed_request"
AUTH_STRATEGIES = frozenset(
    {
        AUTH_API_KEY,
        AUTH_BEARER,
        AUTH_OAUTH2,
        AUTH_SERVICE_ACCOUNT,
        AUTH_BASIC,
        AUTH_SIGNED,
    }
)

# Secret backends
SECRET_BACKEND_ENV = "env"
SECRET_BACKEND_ENCRYPTED_LOCAL = "encrypted_local"
SECRET_BACKEND_EXTERNAL = "external"
SECRET_BACKEND_MEMORY = "memory"


@dataclass(frozen=True)
class IntegrationCredentialRef:
    """Pointer to a secret — never contains plaintext values."""

    credential_id: str
    tenant_id: str
    secret_backend: str
    secret_ref: str
    credential_type: str = AUTH_API_KEY
    version: int = 1
    rotation_state: str = CREDENTIAL_ACTIVE
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not self.credential_id or not self.secret_ref:
            raise ValueError("integration_credential_ref_incomplete")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "metadata", _meta(self.metadata))
        if self.rotation_state not in CREDENTIAL_STATES:
            raise ValueError("invalid_credential_rotation_state")


@dataclass(frozen=True)
class TimeoutPolicy:
    connect_seconds: float = 5.0
    read_seconds: float = 15.0
    total_seconds: float = 30.0

    def __post_init__(self):
        if self.total_seconds <= 0 or self.connect_seconds <= 0 or self.read_seconds <= 0:
            raise ValueError("timeout_policy_invalid")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.5
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504)
    retry_writes: bool = False

    def __post_init__(self):
        object.__setattr__(self, "retry_on_status", tuple(self.retry_on_status))
        if self.max_attempts < 1 or self.max_attempts > 10:
            raise ValueError("retry_policy_invalid")


@dataclass(frozen=True)
class CircuitBreakerPolicy:
    failure_threshold: int = 5
    window_seconds: float = 60.0
    cooldown_seconds: float = 30.0
    half_open_probe_limit: int = 1

    def __post_init__(self):
        if self.failure_threshold < 1:
            raise ValueError("circuit_breaker_policy_invalid")


@dataclass(frozen=True)
class HealthPolicy:
    check_interval_seconds: float = 60.0
    timeout_seconds: float = 5.0
    require_auth_probe: bool = False


@dataclass(frozen=True)
class IntegrationDescriptor:
    """Tenant-scoped integration config — no plaintext secrets."""

    integration_id: str
    tenant_id: str
    provider: str
    integration_type: str
    adapter_id: str
    enabled: bool = False
    environment: str = "production"
    auth_strategy: str = AUTH_API_KEY
    credential_ref: str = ""
    read_capabilities: tuple[str, ...] = ()
    write_capabilities: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()
    health_policy: HealthPolicy = field(default_factory=HealthPolicy)
    timeout_policy: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    circuit_breaker_policy: CircuitBreakerPolicy = field(
        default_factory=CircuitBreakerPolicy
    )
    safe_settings: Mapping[str, object] = field(default_factory=dict)
    base_url: str = ""
    allowed_hosts: tuple[str, ...] = ()
    ip_allowlist: tuple[str, ...] = ()
    version: int = 1
    created_at: datetime = field(default_factory=_utc)
    updated_at: datetime = field(default_factory=_utc)

    def __post_init__(self):
        if not self.integration_id or not self.adapter_id or not self.provider:
            raise ValueError("integration_descriptor_incomplete")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "read_capabilities", tuple(self.read_capabilities))
        object.__setattr__(self, "write_capabilities", tuple(self.write_capabilities))
        object.__setattr__(self, "allowed_operations", tuple(self.allowed_operations))
        object.__setattr__(self, "allowed_hosts", tuple(h.lower() for h in self.allowed_hosts))
        object.__setattr__(self, "ip_allowlist", tuple(self.ip_allowlist))
        object.__setattr__(self, "safe_settings", _meta(self.safe_settings))
        if self.auth_strategy not in AUTH_STRATEGIES:
            raise ValueError("unsupported_auth_strategy")


@dataclass(frozen=True)
class IntegrationHealth:
    status: str = HEALTH_UNKNOWN
    last_check: datetime | None = None
    latency_ms: float | None = None
    error_code: str = ""
    consecutive_failures: int = 0
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in HEALTH_STATES:
            raise ValueError("invalid_health_status")
        object.__setattr__(self, "details", _meta(self.details))


@dataclass(frozen=True)
class IntegrationOperationContext:
    tenant_id: str
    integration_id: str
    operation: str
    request_id: str
    workflow_id: str = ""
    task_id: str = ""
    user_id: str = ""
    service_identity: str = ""
    idempotency_key: str = ""
    capabilities: tuple[str, ...] = ()
    is_write: bool = False

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


@dataclass(frozen=True)
class OAuthTokenBundle:
    """In-memory OAuth material — never persist plaintext via public APIs."""

    access_token: str
    refresh_token: str = ""
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    token_type: str = "Bearer"

    def expired(self, *, skew_seconds: float = 30.0) -> bool:
        if self.expires_at is None:
            return False
        now = _utc()
        return (self.expires_at.timestamp() - skew_seconds) <= now.timestamp()


@dataclass(frozen=True)
class WebhookEnvelope:
    provider: str
    integration_id: str
    tenant_id: str
    event_id: str
    event_type: str
    occurred_at: datetime | None
    received_at: datetime
    payload_ref: str = ""
    signature_verified: bool = False
    idempotency_status: str = "new"
    normalized_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "normalized_metadata", _meta(self.normalized_metadata))
