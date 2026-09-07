"""Product access policy — tenant-scoped default deny (mirrors content_intel.access)."""

from __future__ import annotations

from product_intel.errors import ProductCrossTenantError
from security.tenant import normalize_tenant_id, tenants_match


class ProductAccessPolicy:
    def allow(self, *, requesting_tenant: str, target_tenant: str) -> bool:
        return tenants_match(requesting_tenant, target_tenant)

    def require(self, *, requesting_tenant: str, target_tenant: str) -> None:
        if not self.allow(requesting_tenant=requesting_tenant, target_tenant=target_tenant):
            raise ProductCrossTenantError()

    def normalize(self, tenant_id: str | None) -> str:
        return normalize_tenant_id(tenant_id)
