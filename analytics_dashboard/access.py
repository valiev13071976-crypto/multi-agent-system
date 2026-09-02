"""Analytics dashboard authorization."""

from __future__ import annotations

from analytics_dashboard.errors import AnalyticsError, FORBIDDEN, TENANT_SCOPE_VIOLATION
from security.identity import RequestSecurityContext
from security.rbac import PERM_OPS_COST_READ, PERM_OPS_READ, RBACPolicy

PERM_ANALYTICS_READ = "analytics:read"
PERM_ANALYTICS_FINOPS = "analytics:finops.read"

_ANALYTICS_READ_ROLES = frozenset({"user", "approver", "admin", "tenant_admin", "operator", "viewer"})
_FINOPS_ROLES = frozenset({"admin", "tenant_admin", "operator"})


class AnalyticsAccessPolicy:
    def require_read(self, ctx: RequestSecurityContext, *, tenant_id: str) -> None:
        self._require_tenant(ctx, tenant_id)
        roles = {r.lower() for r in (ctx.roles or ())}
        if roles & _ANALYTICS_READ_ROLES or PERM_OPS_READ in RBACPolicy().permissions_for_roles(ctx.roles or ()):
            return
        if PERM_ANALYTICS_READ in RBACPolicy().permissions_for_roles(ctx.roles or ()):
            return
        raise AnalyticsError(FORBIDDEN, "analytics_read_forbidden")

    def require_finops(self, ctx: RequestSecurityContext, *, tenant_id: str) -> None:
        self.require_read(ctx, tenant_id=tenant_id)
        roles = {r.lower() for r in (ctx.roles or ())}
        perms = RBACPolicy().permissions_for_roles(ctx.roles or ())
        if roles & _FINOPS_ROLES or PERM_OPS_COST_READ in perms or PERM_ANALYTICS_FINOPS in perms:
            return
        raise AnalyticsError(FORBIDDEN, "analytics_finops_forbidden")

    def _require_tenant(self, ctx: RequestSecurityContext, tenant_id: str) -> None:
        if not tenant_id:
            raise AnalyticsError(TENANT_SCOPE_VIOLATION, "tenant_required")
        if ctx.tenant_id and ctx.tenant_id != tenant_id:
            raise AnalyticsError(TENANT_SCOPE_VIOLATION, "cross_tenant_forbidden")
