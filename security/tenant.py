"""Tenant scoping helpers — execution keys, legacy migration."""

from __future__ import annotations

from security.config import DEFAULT_LEGACY_TENANT


class MissingTenantError(ValueError):
    """Raised when a new execution/write requires tenant_id but none was provided."""

    def __init__(self, message: str = "tenant_id required for new execution"):
        self.error = "missing_tenant"
        super().__init__(message)


def normalize_tenant_id(tenant_id: str | None) -> str:
    """Legacy-compatible normalize for reads / migration (may yield legacy-default)."""

    raw = str(tenant_id or "").strip()
    return raw or DEFAULT_LEGACY_TENANT


def require_tenant_id(tenant_id: str | None) -> str:
    """Fail-closed tenant for new authenticated/analyze execution writes.

    Blank/missing tenants must not silently collapse into legacy-default.
    Explicit non-empty values (including an intentional legacy-default string
    from auth-disabled defaults) are accepted as-is.
    """

    raw = str(tenant_id or "").strip()
    if not raw:
        raise MissingTenantError()
    return raw


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


def scope_tenant_ref(tenant_ref: str | None) -> str:
    """Normalized tenant for MemoryScope / persistence queries."""
    return normalize_tenant_id(tenant_ref)
