from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from autonomy.models import sanitize_metadata, utc_now


ACTIVATION_DISABLED = "disabled"
ACTIVATION_CONFIGURED = "configured"
ACTIVATION_DRY_RUN = "dry_run"
ACTIVATION_READY = "ready"
ACTIVATION_BLOCKED = "blocked"
ACTIVATION_ERROR = "error"
ACTIVATION_STATES = (
    ACTIVATION_DISABLED,
    ACTIVATION_CONFIGURED,
    ACTIVATION_DRY_RUN,
    ACTIVATION_READY,
    ACTIVATION_BLOCKED,
    ACTIVATION_ERROR,
)

WRITE_PERMISSION_CONFIRMED = "confirmed"
WRITE_PERMISSION_UNCONFIRMED = "unconfirmed"
WRITE_PERMISSION_DENIED = "denied"
WRITE_PERMISSION_UNKNOWN = "unknown"
WRITE_PERMISSION_STATUSES = (
    WRITE_PERMISSION_CONFIRMED,
    WRITE_PERMISSION_UNCONFIRMED,
    WRITE_PERMISSION_DENIED,
    WRITE_PERMISSION_UNKNOWN,
)

READINESS_READY = "ready"
READINESS_PARTIAL = "partial"
READINESS_BLOCKED = "blocked"
READINESS_UNKNOWN = "unknown"
READINESS_STATUSES = (
    READINESS_READY,
    READINESS_PARTIAL,
    READINESS_BLOCKED,
    READINESS_UNKNOWN,
)

PURPOSE_MUTATE = "mutate"
PURPOSE_ROLLBACK = "rollback"
PURPOSE_DRY_RUN = "dry_run"
PURPOSE_PROBE = "probe"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value))


@dataclass(frozen=True)
class OperationalActivationDecision:
    allowed: bool
    dry_run: bool
    blocked: bool
    reason_code: str
    checked_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class DryRunResult:
    would_execute: bool
    would_change: bool
    current_state_known: bool
    intended_operation: str
    resource_ref: str
    reason_code: str
    checked_at: datetime
    would_require_approval: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))


@dataclass(frozen=True)
class RepositoryReadiness:
    repository_ref: str
    accessible: bool
    authenticated: bool
    reason_code: str
    write_permission_status: str = WRITE_PERMISSION_UNCONFIRMED


@dataclass(frozen=True)
class GitHubReadinessResult:
    status: str
    authenticated: bool
    repository_accessible: bool
    write_permission_status: str
    checked_at: datetime
    expires_at: datetime | None
    repository_results: tuple[RepositoryReadiness, ...]
    reason_code: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in READINESS_STATUSES:
            raise ValueError("invalid_readiness_status")
        if self.write_permission_status not in WRITE_PERMISSION_STATUSES:
            raise ValueError("invalid_write_permission_status")
        object.__setattr__(self, "metadata", _meta(self.metadata))
        object.__setattr__(self, "repository_results", tuple(self.repository_results))

    def for_repository(self, owner: str, repo: str) -> RepositoryReadiness | None:
        key = f"{owner}/{repo}".lower()
        for row in self.repository_results:
            if row.repository_ref.lower() == key:
                return row
        return None


@dataclass(frozen=True)
class SideEffectRuntimeHealth:
    adapter_id: str
    activation_state: str
    configured: bool
    registered: bool
    dry_run: bool
    kill_switch: bool
    readiness_status: str | None
    last_probe_at: datetime | None
    reason_code: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))


@runtime_checkable
class SideEffectActivationProvider(Protocol):
    def evaluate(
        self, action, adapter_descriptor, *, purpose: str = PURPOSE_MUTATE, now=None
    ) -> OperationalActivationDecision: ...

    def health(self) -> SideEffectRuntimeHealth: ...
