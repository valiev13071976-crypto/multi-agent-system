"""Canonical typed identity contracts — no secrets in persisted identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from security.config import ALL_ROLES


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    tenant_id: str
    roles: tuple[str, ...] = ()
    status: str = "active"
    auth_method: str = "api_key"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not self.user_id:
            raise ValueError("user_id required")
        if not self.tenant_id:
            raise ValueError("tenant_id required")
        for role in self.roles:
            if role not in ALL_ROLES:
                raise ValueError(f"invalid_role:{role}")
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "metadata", _meta(self.metadata))

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class TenantIdentity:
    tenant_id: str
    status: str = "active"
    plan_tier: str = "standard"
    security_policy_ref: str = "default"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not self.tenant_id:
            raise ValueError("tenant_id required")
        object.__setattr__(self, "metadata", _meta(self.metadata))

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class RequestSecurityContext:
    """Trusted security context attached to each authenticated HTTP request."""

    user_id: str
    tenant_id: str
    roles: tuple[str, ...]
    request_id: str
    auth_method: str = "api_key"
    session_id: str | None = None
    source_ip: str | None = None
    mfa_verified: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not self.user_id or not self.tenant_id:
            raise ValueError("user_id and tenant_id required")
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "metadata", _meta(self.metadata))

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def actor_ref(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"

    def memory_scope_kwargs(self) -> dict:
        return {
            "scope_type": "workspace",
            "scope_id": self.tenant_id,
            "tenant_ref": self.tenant_id,
            "actor_ref": self.actor_ref(),
        }
