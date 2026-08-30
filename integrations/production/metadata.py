"""Provider metadata contract for production integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VERIFICATION_CODE = "CODE_VERIFIED"
VERIFICATION_CONFIG = "CONFIG_VERIFIED"
VERIFICATION_LIVE = "LIVE_VERIFIED"
VERIFICATION_OPERATOR = "OPERATOR_ACTION_REQUIRED"
VERIFICATION_NOT_ENABLED = "NOT_ENABLED"


@dataclass
class ProviderMetadata:
    provider_id: str
    provider_type: str
    enabled: bool
    configured: bool
    verification_status: str
    capabilities: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    retry_policy: str = "bounded_exponential"
    rate_limit_policy: str = "provider_governor"
    health_state: str = "unknown"
    credential_ref: str = ""
    production_mode: bool = False
    last_check_at: str = ""
    last_success_at: str = ""
    last_failure_category: str = ""
    circuit_state: str = "closed"
    webhook: bool = False
    tenant_scope: str = "global"
    live_evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "enabled": self.enabled,
            "configured": self.configured,
            "verification_status": self.verification_status,
            "capabilities": list(self.capabilities),
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": self.retry_policy,
            "rate_limit_policy": self.rate_limit_policy,
            "health_state": self.health_state,
            "credential_ref": self.credential_ref,
            "production_mode": self.production_mode,
            "last_check_at": self.last_check_at,
            "last_success_at": self.last_success_at,
            "last_failure_category": self.last_failure_category,
            "circuit_state": self.circuit_state,
            "webhook": self.webhook,
            "tenant_scope": self.tenant_scope,
            "live_evidence": dict(self.live_evidence),
        }
