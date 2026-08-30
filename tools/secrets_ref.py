"""SecretReference — never put plaintext secrets in payloads/logs."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata


@dataclass(frozen=True)
class SecretReference:
    """Opaque reference to a server-side secret (no plaintext)."""

    secret_ref: str
    provider: str = ""
    integration_id: str = ""
    tenant_id: str = ""
    metadata: Mapping[str, object] | None = None

    def __post_init__(self):
        ref = str(self.secret_ref or "").strip()
        if not ref:
            raise ValueError("secret_ref_required")
        if ref.lower().startswith(("password=", "token=", "bearer ", "sk-", "api_key=")):
            raise ValueError("plaintext_secret_rejected")
        object.__setattr__(self, "secret_ref", ref)
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(sanitize_metadata(dict(self.metadata or {}))),
        )

    def as_dict(self) -> dict:
        return {
            "secret_ref": self.secret_ref,
            "provider": self.provider,
            "integration_id": self.integration_id,
            "tenant_id": self.tenant_id,
            # Never include resolved secret material
            "has_plaintext": False,
        }

    def __repr__(self) -> str:
        return f"SecretReference(secret_ref={self.secret_ref!r}, provider={self.provider!r})"


def ensure_secret_ref(value) -> SecretReference | None:
    """Coerce mapping/string into SecretReference; reject plaintext-looking values."""
    if value is None or value == "":
        return None
    if isinstance(value, SecretReference):
        return value
    if isinstance(value, Mapping):
        return SecretReference(
            secret_ref=str(value.get("secret_ref") or value.get("ref") or ""),
            provider=str(value.get("provider") or ""),
            integration_id=str(value.get("integration_id") or ""),
            tenant_id=str(value.get("tenant_id") or ""),
            metadata={k: v for k, v in value.items() if k not in {"secret", "password", "token", "api_key"}},
        )
    return SecretReference(secret_ref=str(value))
