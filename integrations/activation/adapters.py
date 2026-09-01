"""Fixture provider adapters for activation — no network."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class FixtureAdapterState:
    auth_ok: bool = True
    permission_ok: bool = True
    rate_limited: bool = False
    timeout: bool = False
    unavailable: bool = False
    degraded: bool = False
    writes: dict[str, dict] = field(default_factory=dict)
    reads: list[dict] = field(default_factory=list)
    pages_served: int = 0
    max_pages: int = 5


class FixtureProviderAdapter:
    """Deterministic FIXTURE adapter simulating verify/read/write/errors."""

    def __init__(self, provider_id: str, *, state: FixtureAdapterState | None = None):
        self.provider_id = provider_id
        self.live = False
        self.environment = "FIXTURE"
        self.state = state or FixtureAdapterState()

    def verify(self, *, credential_ref: str) -> dict:
        if self.state.unavailable:
            return {"ok": False, "category": "INTEGRATION_PROVIDER_UNAVAILABLE"}
        if self.state.timeout:
            return {"ok": False, "category": "INTEGRATION_TIMEOUT"}
        if not self.state.auth_ok or not credential_ref or credential_ref == "secret:invalid":
            return {"ok": False, "category": "INTEGRATION_AUTH_FAILED"}
        if not self.state.permission_ok:
            return {"ok": False, "category": "INTEGRATION_PERMISSION_DENIED"}
        return {
            "ok": True,
            "authentication_valid": True,
            "required_capabilities_available": True,
            "provider_identity": f"fixture:{self.provider_id}",
            "destructive": False,
        }

    def health(self) -> dict:
        if self.state.unavailable:
            return {"status": "UNHEALTHY", "error_category": "INTEGRATION_PROVIDER_UNAVAILABLE"}
        if self.state.rate_limited:
            return {"status": "DEGRADED", "error_category": "INTEGRATION_RATE_LIMITED"}
        if self.state.degraded:
            return {"status": "DEGRADED", "error_category": "degraded"}
        if not self.state.auth_ok:
            return {"status": "UNHEALTHY", "error_category": "INTEGRATION_AUTH_FAILED"}
        return {"status": "HEALTHY", "error_category": ""}

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._raise_if_bad()
        params = params or {}
        page = int(params.get("page") or 1)
        if page > self.state.max_pages:
            return {"items": [], "next_page": None, "page": page, "bounded": True}
        self.state.pages_served += 1
        items = [{"id": f"{self.provider_id}-order-{page}-{i}", "status": "NEW"} for i in range(2)]
        next_page = page + 1 if page < self.state.max_pages else None
        self.state.reads.append({"capability": capability, "page": page})
        return {
            "items": items,
            "next_page": next_page,
            "page": page,
            "bounded": True,
            "mode": "FIXTURE",
            "live": False,
        }

    def write(self, *, capability: str, payload: dict, idempotency_key: str, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._raise_if_bad()
        if idempotency_key in self.state.writes:
            return {**self.state.writes[idempotency_key], "idempotent": True}
        out = {
            "status": "WRITE_ACCEPTED",
            "write_id": str(uuid.uuid4()),
            "capability": capability,
            "mode": "FIXTURE",
            "live": False,
            "verified": "VERIFIED",
            "idempotent": False,
            "payload_summary": {"keys": sorted(payload.keys())},
        }
        self.state.writes[idempotency_key] = out
        return out

    def _raise_if_bad(self) -> None:
        from integrations.activation.errors import (
            IntegrationAuthFailedError,
            IntegrationPermissionDeniedError,
            IntegrationProviderUnavailableError,
            IntegrationRateLimitedError,
            IntegrationTimeoutNormalizedError,
        )

        if self.state.unavailable:
            raise IntegrationProviderUnavailableError()
        if self.state.timeout:
            raise IntegrationTimeoutNormalizedError()
        if self.state.rate_limited:
            raise IntegrationRateLimitedError()
        if not self.state.auth_ok:
            raise IntegrationAuthFailedError()
        if not self.state.permission_ok:
            raise IntegrationPermissionDeniedError()
