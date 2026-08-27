"""Development/test secrets backends — never auto-promote to production-secure."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Mapping

from integrations.contracts import (
    CREDENTIAL_ACTIVE,
    CREDENTIAL_REVOKED,
    CREDENTIAL_ROTATING,
    SECRET_BACKEND_ENV,
    SECRET_BACKEND_MEMORY,
)
from integrations.errors import (
    CredentialInvalidError,
    CredentialMissingError,
    SecretBackendUnavailableError,
)
from integrations.secrets import SecretHandle, SecretMetadata
from security.tenant import normalize_tenant_id


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class MemorySecretsBackend:
    """In-process secrets for unit tests. Not production-capable."""

    backend_id = SECRET_BACKEND_MEMORY
    production_capable = False

    def __init__(self):
        self._lock = threading.RLock()
        self._values: dict[tuple[str, str, int], str] = {}
        self._meta: dict[tuple[str, str], SecretMetadata] = {}
        self._active_version: dict[tuple[str, str], int] = {}

    def get_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> str | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            meta = self._meta.get((tenant, secret_ref))
            if meta is None:
                return None
            if meta.rotation_state in {CREDENTIAL_REVOKED, "invalid"}:
                raise CredentialInvalidError("credential_invalid")
            ver = version if version is not None else self._active_version.get((tenant, secret_ref), 1)
            return self._values.get((tenant, secret_ref, ver))

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
        if not value:
            raise CredentialMissingError("credential_missing")
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            ver = int(version or (self._active_version.get((tenant, secret_ref), 0) + 1))
            self._values[(tenant, secret_ref, ver)] = value
            self._active_version[(tenant, secret_ref)] = ver
            now = _utc()
            self._meta[(tenant, secret_ref)] = SecretMetadata(
                secret_ref=secret_ref,
                tenant_id=tenant,
                version=ver,
                credential_type=credential_type,
                rotation_state=CREDENTIAL_ACTIVE,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
                backend=self.backend_id,
                extra=dict(metadata or {}),
            )
            return SecretHandle(
                secret_ref=secret_ref, tenant_id=tenant, version=ver, backend=self.backend_id
            )

    def delete_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            if version is None:
                for key in list(self._values):
                    if key[0] == tenant and key[1] == secret_ref:
                        del self._values[key]
                self._meta.pop((tenant, secret_ref), None)
                self._active_version.pop((tenant, secret_ref), None)
            else:
                self._values.pop((tenant, secret_ref, int(version)), None)

    def rotate_secret(
        self,
        *,
        tenant_id: str,
        secret_ref: str,
        new_value: str,
        keep_previous: bool = True,
    ) -> SecretHandle:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            meta = self._meta.get((tenant, secret_ref))
            if meta is None:
                raise CredentialMissingError("credential_missing")
            self.set_rotation_state(
                tenant_id=tenant, secret_ref=secret_ref, state=CREDENTIAL_ROTATING
            )
            handle = self.put_secret(
                tenant_id=tenant,
                secret_ref=secret_ref,
                value=new_value,
                credential_type=meta.credential_type,
            )
            if not keep_previous and meta.version != handle.version:
                self.delete_secret(
                    tenant_id=tenant, secret_ref=secret_ref, version=meta.version
                )
            self.set_rotation_state(
                tenant_id=tenant, secret_ref=secret_ref, state=CREDENTIAL_ACTIVE
            )
            return handle

    def metadata(self, *, tenant_id: str, secret_ref: str) -> SecretMetadata | None:
        return self._meta.get((normalize_tenant_id(tenant_id), secret_ref))

    def set_rotation_state(
        self, *, tenant_id: str, secret_ref: str, state: str, version: int | None = None
    ) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            meta = self._meta.get((tenant, secret_ref))
            if meta is None:
                raise CredentialMissingError("credential_missing")
            self._meta[(tenant, secret_ref)] = SecretMetadata(
                secret_ref=meta.secret_ref,
                tenant_id=meta.tenant_id,
                version=version or meta.version,
                credential_type=meta.credential_type,
                rotation_state=state,
                created_at=meta.created_at,
                updated_at=_utc(),
                expires_at=meta.expires_at,
                backend=meta.backend,
                extra=dict(meta.extra),
            )

    def health(self) -> dict:
        return {
            "backend": self.backend_id,
            "status": "healthy",
            "production_capable": False,
            "mode": "test",
        }


class EnvSecretsBackend:
    """
    Resolve secrets from environment / injected env map.
    Explicit development mode — not production-capable by itself.
    """

    backend_id = SECRET_BACKEND_ENV
    production_capable = False

    def __init__(self, env: dict | None = None, *, prefix: str = "INTEGRATION_SECRET_"):
        self._env = env if env is not None else os.environ
        self._prefix = prefix
        self._memory = MemorySecretsBackend()
        self._revoked: set[tuple[str, str]] = set()

    def _env_key(self, tenant_id: str, secret_ref: str) -> str:
        safe = secret_ref.replace(".", "_").replace("-", "_").upper()
        return f"{self._prefix}{normalize_tenant_id(tenant_id)}_{safe}".upper()

    def get_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> str | None:
        tenant = normalize_tenant_id(tenant_id)
        if (tenant, secret_ref) in self._revoked:
            raise CredentialInvalidError("credential_invalid")
        mem = self._memory.get_secret(tenant_id=tenant, secret_ref=secret_ref, version=version)
        if mem is not None:
            return mem
        raw = self._env.get(self._env_key(tenant, secret_ref))
        if raw is None or not str(raw).strip():
            # Also allow plain INTEGRATION_SECRET_<REF>
            alt = self._env.get(f"{self._prefix}{secret_ref.replace('.', '_').upper()}")
            if alt is None or not str(alt).strip():
                return None
            return str(alt)
        return str(raw)

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
        # Dev: store in overlay memory (do not mutate process env permanently)
        return self._memory.put_secret(
            tenant_id=tenant_id,
            secret_ref=secret_ref,
            value=value,
            credential_type=credential_type,
            version=version,
            expires_at=expires_at,
            metadata=metadata,
        )

    def delete_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> None:
        self._memory.delete_secret(tenant_id=tenant_id, secret_ref=secret_ref, version=version)
        self._revoked.add((normalize_tenant_id(tenant_id), secret_ref))

    def rotate_secret(
        self,
        *,
        tenant_id: str,
        secret_ref: str,
        new_value: str,
        keep_previous: bool = True,
    ) -> SecretHandle:
        return self._memory.rotate_secret(
            tenant_id=tenant_id,
            secret_ref=secret_ref,
            new_value=new_value,
            keep_previous=keep_previous,
        )

    def metadata(self, *, tenant_id: str, secret_ref: str) -> SecretMetadata | None:
        meta = self._memory.metadata(tenant_id=tenant_id, secret_ref=secret_ref)
        if meta is not None:
            return meta
        if self.get_secret(tenant_id=tenant_id, secret_ref=secret_ref) is None:
            return None
        return SecretMetadata(
            secret_ref=secret_ref,
            tenant_id=normalize_tenant_id(tenant_id),
            version=1,
            backend=self.backend_id,
        )

    def set_rotation_state(
        self, *, tenant_id: str, secret_ref: str, state: str, version: int | None = None
    ) -> None:
        if state in {CREDENTIAL_REVOKED, "invalid"}:
            self._revoked.add((normalize_tenant_id(tenant_id), secret_ref))
        try:
            self._memory.set_rotation_state(
                tenant_id=tenant_id, secret_ref=secret_ref, state=state, version=version
            )
        except CredentialMissingError:
            if state not in {CREDENTIAL_REVOKED, "invalid"}:
                raise

    def health(self) -> dict:
        return {
            "backend": self.backend_id,
            "status": "healthy",
            "production_capable": False,
            "mode": "development",
        }


class FailClosedSecretsBackend:
    """Used when production backend is configured but unavailable."""

    backend_id = "fail_closed"
    production_capable = True

    def get_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> str | None:
        raise SecretBackendUnavailableError("secret_backend_unavailable")

    def put_secret(self, **kwargs) -> SecretHandle:
        raise SecretBackendUnavailableError("secret_backend_unavailable")

    def delete_secret(self, **kwargs) -> None:
        raise SecretBackendUnavailableError("secret_backend_unavailable")

    def rotate_secret(self, **kwargs) -> SecretHandle:
        raise SecretBackendUnavailableError("secret_backend_unavailable")

    def metadata(self, **kwargs) -> SecretMetadata | None:
        raise SecretBackendUnavailableError("secret_backend_unavailable")

    def set_rotation_state(self, **kwargs) -> None:
        raise SecretBackendUnavailableError("secret_backend_unavailable")

    def health(self) -> dict:
        return {
            "backend": self.backend_id,
            "status": "unavailable",
            "production_capable": True,
            "fail_closed": True,
        }
