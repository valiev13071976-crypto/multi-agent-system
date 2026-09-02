"""Controlled automation RBAC."""

from __future__ import annotations

from controlled_automation.errors import FORBIDDEN, TENANT_SCOPE_VIOLATION, ControlledAutomationError
from security.identity import RequestSecurityContext
from security.rbac import RBACPolicy

PERM_AUTOMATION_READ = "automation.read"
PERM_AUTOMATION_CREATE = "automation.create"
PERM_AUTOMATION_UPDATE = "automation.update"
PERM_AUTOMATION_ENABLE = "automation.enable"
PERM_AUTOMATION_RUN = "automation.run_now"

_READ_ROLES = frozenset({"user", "approver", "admin", "tenant_admin", "operator", "viewer"})


class ControlledAutomationAccessPolicy:
    def require(self, ctx: RequestSecurityContext, permission: str, *, tenant_id: str) -> None:
        self._require_tenant(ctx, tenant_id)
        roles = {r.lower() for r in (ctx.roles or ())}
        if roles & _READ_ROLES:
            if permission == PERM_AUTOMATION_CREATE and roles == {"viewer"}:
                raise ControlledAutomationError(FORBIDDEN, permission)
            if permission == PERM_AUTOMATION_ENABLE and roles == {"viewer"}:
                raise ControlledAutomationError(FORBIDDEN, permission)
            return
        if permission in RBACPolicy().permissions_for_roles(ctx.roles or ()):
            return
        raise ControlledAutomationError(FORBIDDEN, permission)

    def _require_tenant(self, ctx: RequestSecurityContext, tenant_id: str) -> None:
        if not tenant_id:
            raise ControlledAutomationError(TENANT_SCOPE_VIOLATION, "tenant_required")
        if ctx.tenant_id and ctx.tenant_id != tenant_id:
            raise ControlledAutomationError(TENANT_SCOPE_VIOLATION, "cross_tenant_forbidden")
