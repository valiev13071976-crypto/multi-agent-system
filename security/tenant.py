"""Tenant scoping helpers — execution keys, legacy migration."""

from __future__ import annotations

from security.config import DEFAULT_LEGACY_TENANT


def normalize_tenant_id(tenant_id: str | None) -> str:
    raw = str(tenant_id or "").strip()
    return raw or DEFAULT_LEGACY_TENANT


def workflow_tenant_id(state) -> str:
    """Resolve tenant from WorkflowState (field or legacy metadata)."""
    tid = getattr(state, "tenant_id", None)
    if tid:
        return normalize_tenant_id(tid)
    meta = getattr(state, "metadata", None) or {}
    if isinstance(meta, dict):
        return normalize_tenant_id(meta.get("tenant_id"))
    return DEFAULT_LEGACY_TENANT


def scope_execution_key(tenant_id: str, execution_key: str) -> str:
    """Tenant-scoped queue dedupe key — prevents cross-tenant collisions."""
    tenant = normalize_tenant_id(tenant_id)
    key = str(execution_key or "").strip()
    if not key:
        raise ValueError("execution_key required")
    return f"tenant:{tenant}:exec:{key}"


def tenants_match(expected: str | None, actual: str | None) -> bool:
    return normalize_tenant_id(expected) == normalize_tenant_id(actual)
