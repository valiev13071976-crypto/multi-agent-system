"""Scheduled automation access control."""

from __future__ import annotations

from scheduled_automation.errors import FORBIDDEN, TENANT_SCOPE_VIOLATION, ScheduledAutomationError
from security.identity import RequestSecurityContext
from security.rbac import RBACPolicy

PERM_SCHEDULE_READ = "schedule.read"
PERM_SCHEDULE_CREATE = "schedule.create"
PERM_SCHEDULE_UPDATE = "schedule.update"
PERM_SCHEDULE_ENABLE = "schedule.enable"
PERM_SCHEDULE_RUN_NOW = "schedule.run_now"

_READ_ROLES = frozenset({"user", "approver", "admin", "tenant_admin", "operator", "viewer"})


class ScheduleAccessPolicy:
    def require(self, ctx: RequestSecurityContext, permission: str, *, tenant_id: str) -> None:
        self._require_tenant(ctx, tenant_id)
        roles = {r.lower() for r in (ctx.roles or ())}
        if roles & _READ_ROLES:
            if permission == PERM_SCHEDULE_CREATE and roles == {"viewer"}:
                raise ScheduledAutomationError(FORBIDDEN, permission)
            return
        if permission in RBACPolicy().permissions_for_roles(ctx.roles or ()):
            return
        raise ScheduledAutomationError(FORBIDDEN, permission)

    def _require_tenant(self, ctx: RequestSecurityContext, tenant_id: str) -> None:
        if not tenant_id:
            raise ScheduledAutomationError(TENANT_SCOPE_VIOLATION, "tenant_required")
        if ctx.tenant_id and ctx.tenant_id != tenant_id:
            raise ScheduledAutomationError(TENANT_SCOPE_VIOLATION, "cross_tenant_forbidden")
