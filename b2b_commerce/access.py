"""Tenant access policy for B2B."""

from __future__ import annotations

from b2b_commerce.errors import B2B_ACCESS_DENIED, B2BCommerceError


class B2BAccessPolicy:
    def require(self, *, requesting_tenant: str, target_tenant: str) -> None:
        if requesting_tenant != target_tenant:
            raise B2BCommerceError(B2B_ACCESS_DENIED)

    def require_supplier_binding(self, *, tenant_id: str, supplier_tenant: str) -> None:
        self.require(requesting_tenant=tenant_id, target_tenant=supplier_tenant)

    def require_telegram_binding(self, *, tenant_id: str, binding_tenant: str) -> None:
        self.require(requesting_tenant=tenant_id, target_tenant=binding_tenant)
