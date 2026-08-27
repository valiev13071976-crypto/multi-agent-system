"""Encrypted local secrets store — ciphertext at rest, fail-closed without encryption."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from integrations.contracts import (
    CREDENTIAL_ACTIVE,
    CREDENTIAL_REVOKED,
    CREDENTIAL_ROTATING,
    SECRET_BACKEND_ENCRYPTED_LOCAL,
)
from integrations.errors import (
    CredentialInvalidError,
    CredentialMissingError,
    SecretBackendUnavailableError,
)
from integrations.secrets import SecretHandle, SecretMetadata
from security.encryption import EncryptionService, EncryptionUnavailableError
from security.tenant import normalize_tenant_id

_DDL = """
CREATE TABLE IF NOT EXISTS integration_secrets (
    tenant_id TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    version INTEGER NOT NULL,
    credential_type TEXT NOT NULL,
    rotation_state TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, secret_ref, version)
);
CREATE TABLE IF NOT EXISTS integration_secret_active (
    tenant_id TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    active_version INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, secret_ref)
);
"""


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class EncryptedLocalSecretsBackend:
    """
    Production-capable local backend: AES-GCM ciphertext in SQLite.
    Never stores plaintext secret values.
    """

    backend_id = SECRET_BACKEND_ENCRYPTED_LOCAL
    production_capable = True

    def __init__(
        self,
        *,
        encryption: EncryptionService | None,
        path: str | None = None,
        shared_connection=None,
        require_encryption: bool = True,
    ):
        if require_encryption and encryption is None:
            raise SecretBackendUnavailableError("secret_backend_unavailable")
        self._encryption = encryption
        self._shared = shared_connection
        self._path = path
        self._lock = threading.RLock()
        self._local = threading.local()
        self._owns = shared_connection is None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared.connect()
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not self._path:
                raise SecretBackendUnavailableError("secret_backend_unavailable")
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _commit(self, conn: sqlite3.Connection) -> None:
        if self._shared is not None and hasattr(self._shared, "maybe_autocommit"):
            self._shared.maybe_autocommit()
            return
        conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_DDL)
            self._commit(conn)

    def _encrypt(self, value: str) -> str:
        if self._encryption is None:
            raise SecretBackendUnavailableError("secret_backend_unavailable")
        try:
            payload = self._encryption.encrypt(value)
        except EncryptionUnavailableError as exc:
            raise SecretBackendUnavailableError("secret_backend_unavailable") from exc
        return payload.serialize()

    def _decrypt(self, ciphertext: str) -> str:
        if self._encryption is None:
            raise SecretBackendUnavailableError("secret_backend_unavailable")
        try:
            return self._encryption.decrypt(ciphertext)
        except Exception as exc:
            raise CredentialInvalidError("credential_invalid") from exc

    def get_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> str | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            if version is None:
                row = conn.execute(
                    "SELECT active_version FROM integration_secret_active "
                    "WHERE tenant_id=? AND secret_ref=?",
                    (tenant, secret_ref),
                ).fetchone()
                if row is None:
                    return None
                version = int(row["active_version"])
            row = conn.execute(
                "SELECT ciphertext, rotation_state FROM integration_secrets "
                "WHERE tenant_id=? AND secret_ref=? AND version=?",
                (tenant, secret_ref, int(version)),
            ).fetchone()
            if row is None:
                return None
            if row["rotation_state"] in {CREDENTIAL_REVOKED, "invalid"}:
                raise CredentialInvalidError("credential_invalid")
            return self._decrypt(row["ciphertext"])

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
        ciphertext = self._encrypt(value)
        with self._lock:
            conn = self._connect()
            if version is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS m FROM integration_secrets "
                    "WHERE tenant_id=? AND secret_ref=?",
                    (tenant, secret_ref),
                ).fetchone()
                version = int(row["m"]) + 1
            now = _utc().isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO integration_secrets("
                "tenant_id, secret_ref, version, credential_type, rotation_state, "
                "ciphertext, expires_at, created_at, updated_at, metadata_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    secret_ref,
                    int(version),
                    credential_type,
                    CREDENTIAL_ACTIVE,
                    ciphertext,
                    _iso(expires_at),
                    now,
                    now,
                    json.dumps(dict(metadata or {}), separators=(",", ":"), sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO integration_secret_active("
                "tenant_id, secret_ref, active_version) VALUES (?,?,?)",
                (tenant, secret_ref, int(version)),
            )
            self._commit(conn)
            return SecretHandle(
                secret_ref=secret_ref,
                tenant_id=tenant,
                version=int(version),
                backend=self.backend_id,
            )

    def delete_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            if version is None:
                conn.execute(
                    "DELETE FROM integration_secrets WHERE tenant_id=? AND secret_ref=?",
                    (tenant, secret_ref),
                )
                conn.execute(
                    "DELETE FROM integration_secret_active WHERE tenant_id=? AND secret_ref=?",
                    (tenant, secret_ref),
                )
            else:
                conn.execute(
                    "DELETE FROM integration_secrets WHERE tenant_id=? AND secret_ref=? AND version=?",
                    (tenant, secret_ref, int(version)),
                )
            self._commit(conn)

    def rotate_secret(
        self,
        *,
        tenant_id: str,
        secret_ref: str,
        new_value: str,
        keep_previous: bool = True,
    ) -> SecretHandle:
        meta = self.metadata(tenant_id=tenant_id, secret_ref=secret_ref)
        if meta is None:
            raise CredentialMissingError("credential_missing")
        self.set_rotation_state(
            tenant_id=tenant_id, secret_ref=secret_ref, state=CREDENTIAL_ROTATING
        )
        handle = self.put_secret(
            tenant_id=tenant_id,
            secret_ref=secret_ref,
            value=new_value,
            credential_type=meta.credential_type,
        )
        if not keep_previous:
            self.delete_secret(
                tenant_id=tenant_id, secret_ref=secret_ref, version=meta.version
            )
        self.set_rotation_state(
            tenant_id=tenant_id, secret_ref=secret_ref, state=CREDENTIAL_ACTIVE
        )
        return handle

    def metadata(self, *, tenant_id: str, secret_ref: str) -> SecretMetadata | None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            active = conn.execute(
                "SELECT active_version FROM integration_secret_active "
                "WHERE tenant_id=? AND secret_ref=?",
                (tenant, secret_ref),
            ).fetchone()
            if active is None:
                return None
            row = conn.execute(
                "SELECT * FROM integration_secrets WHERE tenant_id=? AND secret_ref=? AND version=?",
                (tenant, secret_ref, int(active["active_version"])),
            ).fetchone()
            if row is None:
                return None
            return SecretMetadata(
                secret_ref=secret_ref,
                tenant_id=tenant,
                version=int(row["version"]),
                credential_type=row["credential_type"],
                rotation_state=row["rotation_state"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                expires_at=(
                    datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
                ),
                backend=self.backend_id,
                extra=json.loads(row["metadata_json"] or "{}"),
            )

    def set_rotation_state(
        self, *, tenant_id: str, secret_ref: str, state: str, version: int | None = None
    ) -> None:
        tenant = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            if version is None:
                row = conn.execute(
                    "SELECT active_version FROM integration_secret_active "
                    "WHERE tenant_id=? AND secret_ref=?",
                    (tenant, secret_ref),
                ).fetchone()
                if row is None:
                    raise CredentialMissingError("credential_missing")
                version = int(row["active_version"])
            cur = conn.execute(
                "UPDATE integration_secrets SET rotation_state=?, updated_at=? "
                "WHERE tenant_id=? AND secret_ref=? AND version=?",
                (state, _utc().isoformat(), tenant, secret_ref, int(version)),
            )
            if cur.rowcount == 0:
                raise CredentialMissingError("credential_missing")
            self._commit(conn)

    def close(self) -> None:
        if not self._owns:
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def health(self) -> dict:
        status = "healthy" if self._encryption is not None else "unavailable"
        return {
            "backend": self.backend_id,
            "status": status,
            "production_capable": True,
            "encryption": self._encryption is not None,
        }


class ExternalSecretsBackend:
    """
    Adapter boundary for Vault / AWS SM / Azure KV / GCP SM.
    Without a concrete provider SDK, production mode fails closed.
    """

    backend_id = "external"
    production_capable = True

    def __init__(self, *, provider: str = "vault", available: bool = False, delegate=None):
        self.provider = provider
        self._available = bool(available)
        self._delegate = delegate

    def _ensure(self):
        if self._delegate is not None:
            return
        if not self._available:
            raise SecretBackendUnavailableError("secret_backend_unavailable")

    def get_secret(self, *, tenant_id: str, secret_ref: str, version: int | None = None) -> str | None:
        self._ensure()
        return self._delegate.get_secret(
            tenant_id=tenant_id, secret_ref=secret_ref, version=version
        )

    def put_secret(self, **kwargs) -> SecretHandle:
        self._ensure()
        return self._delegate.put_secret(**kwargs)

    def delete_secret(self, **kwargs) -> None:
        self._ensure()
        return self._delegate.delete_secret(**kwargs)

    def rotate_secret(self, **kwargs) -> SecretHandle:
        self._ensure()
        return self._delegate.rotate_secret(**kwargs)

    def metadata(self, **kwargs) -> SecretMetadata | None:
        self._ensure()
        return self._delegate.metadata(**kwargs)

    def set_rotation_state(self, **kwargs) -> None:
        self._ensure()
        return self._delegate.set_rotation_state(**kwargs)

    def health(self) -> dict:
        if self._delegate is not None:
            h = dict(self._delegate.health())
            h["provider"] = self.provider
            h["backend"] = self.backend_id
            return h
        return {
            "backend": self.backend_id,
            "provider": self.provider,
            "status": "unavailable" if not self._available else "healthy",
            "production_capable": True,
            "fail_closed": not self._available and self._delegate is None,
        }
