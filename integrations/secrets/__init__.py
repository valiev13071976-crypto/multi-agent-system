"""Secrets backend protocol — values exist only briefly in memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SecretMetadata:
    secret_ref: str
    tenant_id: str
    version: int = 1
    credential_type: str = "api_key"
    rotation_state: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    backend: str = ""
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "extra", sanitize_metadata(self.extra or {}))


@dataclass(frozen=True)
class SecretHandle:
    """Opaque handle returned from put — never includes value."""

    secret_ref: str
    tenant_id: str
    version: int
    backend: str


class SecretsBackend(Protocol):
    backend_id: str
    production_capable: bool

    def get_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> str | None:
        ...

    def put_secret(
        self,
        *,
        tenant_id: str,
        secret_ref: str,
        value: str,
        credential_type: str = "api_key",
        version: int | None = None,
        expires_at: datetime | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> SecretHandle:
        ...

    def delete_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> None:
        ...

    def rotate_secret(
        self,
        *,
        tenant_id: str,
        secret_ref: str,
        new_value: str,
        keep_previous: bool = True,
    ) -> SecretHandle:
        ...

    def metadata(self, *, tenant_id: str, secret_ref: str) -> SecretMetadata | None:
        ...

    def set_rotation_state(
        self, *, tenant_id: str, secret_ref: str, state: str, version: int | None = None
    ) -> None:
        ...

    def health(self) -> dict:
        ...
