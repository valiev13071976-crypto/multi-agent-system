"""AI provider metadata for Stage-2 production integration layer."""

from __future__ import annotations

from datetime import datetime, timezone

from agents.provider_registry import PROVIDER_ENV, PROVIDER_IDS
from integrations.production.metadata import (
    VERIFICATION_CODE,
    VERIFICATION_CONFIG,
    VERIFICATION_LIVE,
    VERIFICATION_NOT_ENABLED,
    VERIFICATION_OPERATOR,
    ProviderMetadata,
)
from production_foundation.config import reject_placeholder_secret


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def ai_provider_metadata(env: dict, *, health_tracker=None) -> list[ProviderMetadata]:
    out: list[ProviderMetadata] = []
    for pid in PROVIDER_IDS:
        env_key = next((k for p, k, _ in PROVIDER_ENV if p == pid), f"{pid.upper()}_API_KEY")
        raw = str(env.get(env_key) or "").strip()
        configured = bool(raw) and reject_placeholder_secret(raw)
        enabled = configured or not str(env.get("PANDA_ENV") or "").strip().lower() in {"production", "prod"}
        verification = VERIFICATION_CODE
        if configured:
            verification = VERIFICATION_CONFIG
        elif str(env.get("PANDA_ENV") or "").strip().lower() in {"production", "prod"}:
            verification = VERIFICATION_OPERATOR
        health_state = "unknown"
        circuit_state = "closed"
        if health_tracker is not None:
            snap = health_tracker.snapshot(pid)
            health_state = snap.state
            circuit_state = snap.state
        out.append(
            ProviderMetadata(
                provider_id=pid,
                provider_type="ai",
                enabled=enabled,
                configured=configured,
                verification_status=verification,
                capabilities=("chat", "completion"),
                timeout_seconds=float(env.get(f"{pid.upper()}_TIMEOUT_SECONDS") or 120),
                credential_ref=env_key,
                production_mode=str(env.get("PANDA_ENV") or "").strip().lower() in {"production", "prod"},
                health_state=health_state,
                circuit_state=circuit_state,
                tenant_scope="global",
            )
        )
    return out


def mark_live_verified(meta: ProviderMetadata, *, operation: str, latency_ms: float, resource_id: str = "") -> ProviderMetadata:
    meta.verification_status = VERIFICATION_LIVE
    meta.last_success_at = _utc()
    meta.live_evidence = {
        "operation": operation,
        "timestamp": _utc(),
        "success": True,
        "latency_ms": latency_ms,
        "resource_id": resource_id,
    }
    return meta
