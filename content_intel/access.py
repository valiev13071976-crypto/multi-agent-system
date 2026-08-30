"""Content access policy — tenant-scoped default deny."""

from __future__ import annotations

from content_intel.errors import CONTENT_ACCESS_DENIED, ContentIntelError
from security.tenant import normalize_tenant_id, tenants_match


class ContentAccessPolicy:
    def allow(self, *, requesting_tenant: str, target_tenant: str) -> bool:
        return tenants_match(requesting_tenant, target_tenant)

    def require(self, *, requesting_tenant: str, target_tenant: str) -> None:
        if not self.allow(requesting_tenant=requesting_tenant, target_tenant=target_tenant):
            raise ContentIntelError(CONTENT_CROSS_TENANT)

    def normalize(self, tenant_id: str | None) -> str:
        return normalize_tenant_id(tenant_id)
