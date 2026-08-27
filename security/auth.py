"""API authentication — API keys and bearer tokens (no custom crypto)."""

from __future__ import annotations

import hmac
import os
import uuid
from dataclasses import dataclass

from security.config import (
    AUTH_MODE_DISABLED,
    AUTH_MODE_REQUIRED,
    DEFAULT_LEGACY_TENANT,
    DEFAULT_LEGACY_USER,
    ROLE_OPERATOR,
    ROLE_USER,
    security_auth_mode,
    security_default_tenant,
)
from security.errors import DisabledAccountError, UnauthenticatedError
from security.identity import RequestSecurityContext, TenantIdentity, UserIdentity


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    secret: str
    tenant_id: str
    user_id: str
    roles: tuple[str, ...]
    status: str = "active"


def _parse_api_keys(raw: str) -> tuple[ApiKeyRecord, ...]:
    """Parse PANDA_API_KEYS: key_id|tenant|user|roles|secret;..."""
    out: list[ApiKeyRecord] = []
    for entry in str(raw or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) < 5:
            continue
        key_id, tenant_id, user_id, roles_raw, secret = parts[:5]
        roles = tuple(r.strip() for r in roles_raw.split(",") if r.strip())
        out.append(
            ApiKeyRecord(
                key_id=key_id.strip(),
                secret=secret.strip(),
                tenant_id=tenant_id.strip(),
                user_id=user_id.strip(),
                roles=roles or (ROLE_USER,),
            )
        )
    return tuple(out)


class AuthService:
    """Validate credentials and produce trusted RequestSecurityContext."""

    def __init__(self, *, env: dict | None = None, api_keys: tuple[ApiKeyRecord, ...] | None = None):
        self._env = env
        self._mode = security_auth_mode(env)
        self._default_tenant = security_default_tenant(env)
        if api_keys is not None:
            self._keys = api_keys
        else:
            source = env if env is not None else os.environ
            self._keys = _parse_api_keys(source.get("PANDA_API_KEYS") or "")

    @property
    def mode(self) -> str:
        return self._mode

    def dev_context(self, *, request_id: str | None = None) -> RequestSecurityContext:
        return RequestSecurityContext(
            user_id=DEFAULT_LEGACY_USER,
            tenant_id=self._default_tenant,
            roles=(ROLE_USER, ROLE_OPERATOR),
            request_id=request_id or str(uuid.uuid4()),
            auth_method="disabled",
        )

    def authenticate(
        self,
        *,
        api_key: str | None = None,
        bearer: str | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
    ) -> RequestSecurityContext:
        if self._mode == AUTH_MODE_DISABLED:
            return self.dev_context(request_id=request_id)

        credential = (api_key or bearer or "").strip()
        if not credential:
            raise UnauthenticatedError("credential_required")

        record = self._match_credential(credential)
        if record is None:
            raise UnauthenticatedError("invalid_credential")
        if record.status != "active":
            raise DisabledAccountError("disabled_credential")

        tenant = self._resolve_tenant(record.tenant_id)
        if tenant.status != "active":
            raise DisabledAccountError("disabled_tenant")

        return RequestSecurityContext(
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            roles=record.roles,
            request_id=request_id or str(uuid.uuid4()),
            auth_method="api_key" if api_key else "bearer",
            source_ip=source_ip,
            metadata={"key_id": record.key_id},
        )

    def _match_credential(self, credential: str) -> ApiKeyRecord | None:
        for record in self._keys:
            if hmac.compare_digest(credential, record.secret):
                return record
        return None

    def _resolve_tenant(self, tenant_id: str) -> TenantIdentity:
        # Placeholder tenant registry — extend for multi-tenant admin later.
        return TenantIdentity(tenant_id=tenant_id, status="active")

    def require_production_keys(self) -> None:
        """Fail fast when production mode has no configured keys."""
        if self._mode == AUTH_MODE_REQUIRED and not self._keys:
            raise RuntimeError("SECURITY_AUTH_MODE=required but PANDA_API_KEYS is empty")
