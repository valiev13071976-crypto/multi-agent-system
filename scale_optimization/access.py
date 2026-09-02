"""RBAC for scale optimization management reads."""

from __future__ import annotations

from scale_optimization.errors import FORBIDDEN, TENANT_SCOPE_VIOLATION, ScaleOptimizationError
from security.identity import RequestSecurityContext

PERM_SCALE_READ = "scale.read"
PERM_SCALE_BENCHMARK = "scale.benchmark"

_READ_ROLES = frozenset({"admin", "tenant_admin", "operator", "viewer", "approver", "user"})
_BENCH_ROLES = frozenset({"admin", "tenant_admin", "operator"})


class ScaleOptimizationAccessPolicy:
    def require(self, ctx: RequestSecurityContext, permission: str, *, tenant_id: str | None = None) -> None:
        if tenant_id and ctx.tenant_id and ctx.tenant_id != tenant_id:
            raise ScaleOptimizationError(TENANT_SCOPE_VIOLATION, "cross_tenant_forbidden")
        roles = {r.lower() for r in (ctx.roles or ())}
        if permission == PERM_SCALE_READ and roles & _READ_ROLES:
            return
        if permission == PERM_SCALE_BENCHMARK and roles & _BENCH_ROLES:
            return
        raise ScaleOptimizationError(FORBIDDEN, permission)
