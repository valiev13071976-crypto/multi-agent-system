"""Product authorization — membership and capability enforcement."""

from __future__ import annotations

from saas_product.capabilities import ROLE_PERMISSIONS, PERM_PRODUCT_READ
from saas_product.errors import SAAS_FORBIDDEN, SAAS_NOT_FOUND, SAAS_SCOPE_DENIED, SaaSError
from saas_product.models import MEMBERSHIP_ACTIVE, MembershipRecord
from security.identity import RequestSecurityContext


class ProductAuthorizationPolicy:
    def __init__(self, membership_resolver):
        self._memberships = membership_resolver

    def active_membership(self, ctx: RequestSecurityContext, *, tenant_id: str | None = None) -> MembershipRecord:
        tid = tenant_id or ctx.tenant_id
        rec = self._memberships(ctx.user_id, tid)
        if rec is None:
            raise SaaSError(SAAS_SCOPE_DENIED, message="Not a member of tenant.")
        return rec

    def permissions(self, ctx: RequestSecurityContext, *, tenant_id: str | None = None) -> frozenset[str]:
        mem = self.active_membership(ctx, tenant_id=tenant_id)
        return ROLE_PERMISSIONS.get(mem.role, frozenset())

    def require(self, ctx: RequestSecurityContext, permission: str, *, tenant_id: str | None = None) -> MembershipRecord:
        if permission not in self.permissions(ctx, tenant_id=tenant_id):
            raise SaaSError(SAAS_FORBIDDEN, message=f"Missing capability: {permission}")
        return self.active_membership(ctx, tenant_id=tenant_id)

    def require_product_read(self, ctx: RequestSecurityContext, *, tenant_id: str | None = None) -> MembershipRecord:
        return self.require(ctx, PERM_PRODUCT_READ, tenant_id=tenant_id)

    def assert_tenant_scope(self, ctx: RequestSecurityContext, resource_tenant: str) -> None:
        if resource_tenant != ctx.tenant_id:
            mem = self._memberships(ctx.user_id, resource_tenant)
            if mem is None:
                raise SaaSError(SAAS_SCOPE_DENIED)

    def is_owner(self, ctx: RequestSecurityContext, *, tenant_id: str | None = None) -> bool:
        try:
            mem = self.active_membership(ctx, tenant_id=tenant_id)
            return mem.role == "OWNER"
        except SaaSError:
            return False
