"""Admin authorization policy — capability and tenant scope."""

from __future__ import annotations

from operations_admin.capabilities import (
    OPERATOR_PERMS,
    PLATFORM_ADMIN_PERMS,
    SECURITY_AUDITOR_PERMS,
    TENANT_ADMIN_PERMS,
    VIEWER_PERMS,
    PERM_OPS_READ,
)
from operations_admin.errors import ADMIN_FORBIDDEN, ADMIN_SCOPE_DENIED, AdminError
from security.config import (
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_OPERATOR,
    ROLE_SECURITY_AUDITOR,
    ROLE_TENANT_ADMIN,
    ROLE_USER,
    ROLE_VIEWER,
)
from security.identity import RequestSecurityContext
from security.rbac import RBACPolicy, RBACDenied


ROLE_VIEWER = "viewer"
ROLE_SECURITY_AUDITOR = "security_auditor"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_PLATFORM_ADMIN = "platform_admin"


def profile_for_roles(roles: tuple[str, ...]) -> str:
    if ROLE_PLATFORM_ADMIN in roles or ROLE_ADMIN in roles:
        return "PLATFORM_ADMIN"
    if ROLE_TENANT_ADMIN in roles:
        return "TENANT_ADMIN"
    if ROLE_VIEWER in roles:
        return "VIEWER"
    if ROLE_SECURITY_AUDITOR in roles:
        return "SECURITY_AUDITOR"
    if ROLE_OPERATOR in roles or ROLE_APPROVER in roles:
        return "OPERATOR"
    if ROLE_VIEWER in roles:
        return "VIEWER"
    return "NONE"


def permissions_for_profile(profile: str) -> frozenset[str]:
    if profile == "PLATFORM_ADMIN":
        return PLATFORM_ADMIN_PERMS
    if profile == "TENANT_ADMIN":
        return TENANT_ADMIN_PERMS
    if profile == "OPERATOR":
        return OPERATOR_PERMS
    if profile == "SECURITY_AUDITOR":
        return SECURITY_AUDITOR_PERMS
    if profile == "VIEWER":
        return VIEWER_PERMS
    return frozenset()


class AdminAuthorizationPolicy:
    def __init__(self, rbac: RBACPolicy | None = None):
        self.rbac = rbac or RBACPolicy()

    def profile(self, ctx: RequestSecurityContext) -> str:
        return profile_for_roles(ctx.roles)

    def permissions(self, ctx: RequestSecurityContext) -> frozenset[str]:
        base = permissions_for_profile(self.profile(ctx))
        rbac_perms = self.rbac.permissions_for_roles(ctx.roles)
        # Merge explicit RBAC ops capabilities
        from operations_admin.capabilities import (
            PERM_OPS_APPROVAL,
            PERM_OPS_COST_READ,
            PERM_OPS_COST_WRITE,
            PERM_OPS_READ,
            PERM_OPS_RECOVERY,
            PERM_OPS_ROUTING_WRITE,
            PERM_OPS_SECURITY_READ,
            PERM_OPS_TENANT_READ,
            PERM_OPS_TENANT_WRITE,
            PERM_OPS_WRITE,
        )

        for perm in (
            PERM_OPS_READ,
            PERM_OPS_WRITE,
            PERM_OPS_RECOVERY,
            PERM_OPS_SECURITY_READ,
            PERM_OPS_COST_READ,
            PERM_OPS_COST_WRITE,
            PERM_OPS_TENANT_READ,
            PERM_OPS_TENANT_WRITE,
            PERM_OPS_APPROVAL,
            PERM_OPS_ROUTING_WRITE,
        ):
            if perm in rbac_perms:
                base = base | frozenset({perm})
        if "admin:metadata" in rbac_perms:
            base = base | frozenset({PERM_OPS_READ})
        return base

    def require(self, ctx: RequestSecurityContext, permission: str) -> None:
        if permission not in self.permissions(ctx):
            raise AdminError(ADMIN_FORBIDDEN, message=f"Missing capability: {permission}")

    def assert_tenant_scope(self, ctx: RequestSecurityContext, resource_tenant: str | None) -> None:
        profile = self.profile(ctx)
        if profile in {"PLATFORM_ADMIN", "OPERATOR", "SECURITY_AUDITOR", "VIEWER"}:
            return
        if profile == "TENANT_ADMIN":
            if not resource_tenant or resource_tenant != ctx.tenant_id:
                raise AdminError(ADMIN_SCOPE_DENIED, message="Tenant scope denied.")
            return
        raise AdminError(ADMIN_FORBIDDEN)

    def is_platform_scope(self, ctx: RequestSecurityContext) -> bool:
        return self.profile(ctx) in {"PLATFORM_ADMIN", "OPERATOR", "VIEWER", "SECURITY_AUDITOR"}

    def allowed_tenant_filter(self, ctx: RequestSecurityContext, requested: str | None) -> str | None:
        profile = self.profile(ctx)
        if profile in {"PLATFORM_ADMIN", "OPERATOR", "VIEWER", "SECURITY_AUDITOR"}:
            return requested
        return ctx.tenant_id
