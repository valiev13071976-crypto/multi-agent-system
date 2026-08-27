"""Object-level authorization — tenant boundary + RBAC."""

from __future__ import annotations

from security.errors import ResourceNotFoundError, TenantMismatchError, UnauthorizedError
from security.identity import RequestSecurityContext
from security.rbac import RBACPolicy, RBACDenied
from security.tenant import normalize_tenant_id, workflow_tenant_id


class ResourceAuthorizer:
    """Central object-level checks — use after authentication."""

    def __init__(self, rbac: RBACPolicy | None = None):
        self.rbac = rbac or RBACPolicy()

    def require_permission(self, ctx: RequestSecurityContext, permission: str) -> None:
        try:
            self.rbac.require(ctx.roles, permission)
        except RBACDenied as exc:
            raise UnauthorizedError(exc.reason) from exc

    def authorize_workflow_access(
        self,
        ctx: RequestSecurityContext,
        workflow_state,
        *,
        permission: str,
    ) -> None:
        self.require_permission(ctx, permission)
        owner = workflow_tenant_id(workflow_state)
        if not self._tenant_allows(ctx.tenant_id, owner):
            # IDOR: return safe not-found
            raise ResourceNotFoundError("workflow_not_found")

    def _tenant_allows(self, requester: str, owner: str) -> bool:
        req = normalize_tenant_id(requester)
        own = normalize_tenant_id(owner)
        if req == own:
            return True
        return False

    def assert_tenant_match(
        self, ctx: RequestSecurityContext, resource_tenant_id: str | None
    ) -> None:
        if not self._tenant_allows(ctx.tenant_id, resource_tenant_id):
            raise ResourceNotFoundError("resource_not_found")
