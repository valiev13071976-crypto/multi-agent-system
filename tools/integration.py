"""Tenant-scoped integration configuration and credential references.

Compatibility layer — production resolution goes through integrations.runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


@dataclass(frozen=True)
class IntegrationCredentialRef:
    """Server-side credential pointer — never contains plaintext secrets."""

    integration_id: str
    tenant_id: str
    credential_key: str
    provider: str

    def __post_init__(self):
        if not self.integration_id or not self.credential_key:
            raise ValueError("integration_credential_ref_incomplete")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))


@dataclass(frozen=True)
class IntegrationConfig:
    integration_id: str
    tenant_id: str
    adapter_id: str
    provider: str
    enabled: bool = False
    credential_ref: str = ""
    settings: Mapping[str, object] = field(default_factory=dict)
    health_status: str = "unknown"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not self.integration_id or not self.adapter_id:
            raise ValueError("integration_config_incomplete")
        object.__setattr__(self, "tenant_id", normalize_tenant_id(self.tenant_id))
        object.__setattr__(self, "settings", _meta(self.settings))


class IntegrationCredentialStore:
    """
    In-process tenant-scoped credential resolver for unit tests / bootstrap fallback.
    Production compose injects IntegrationCredentialBridge instead.
    """

    def __init__(self):
        self._configs: dict[tuple[str, str], IntegrationConfig] = {}
        self._secrets: dict[tuple[str, str], str] = {}

    def register(self, config: IntegrationConfig, *, secret: str | None = None) -> None:
        key = (config.tenant_id, config.integration_id)
        self._configs[key] = config
        if secret:
            self._secrets[(config.tenant_id, config.credential_ref or config.integration_id)] = (
                secret
            )

    def get_config(self, tenant_id: str, integration_id: str) -> IntegrationConfig | None:
        return self._configs.get((normalize_tenant_id(tenant_id), integration_id))

    def resolve_secret(self, ref: IntegrationCredentialRef) -> str | None:
        tenant = normalize_tenant_id(ref.tenant_id)
        if tenant != ref.tenant_id and normalize_tenant_id(ref.tenant_id) != tenant:
            return None
        # Cross-tenant: ref must match lookup tenant semantics
        return self._secrets.get((tenant, ref.credential_key))

    def assert_tenant_access(self, requester_tenant: str, integration_id: str) -> IntegrationConfig:
        cfg = self.get_config(requester_tenant, integration_id)
        if cfg is None or not cfg.enabled:
            from tools.errors import ToolAuthFailedError

            raise ToolAuthFailedError("integration_not_available")
        return cfg
