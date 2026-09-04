"""Tenant-scoped in-memory package store — fail closed across tenants."""

from __future__ import annotations

from product_content.contracts import ProductContentPackage
from product_content.errors import CONTENT_ACCESS_DENIED, CONTENT_PACKAGE_NOT_FOUND, ProductContentError
from security.tenant import require_tenant_id


class ProductContentStore:
    def __init__(self) -> None:
        self._packages: dict[str, dict[str, ProductContentPackage]] = {}
        self._by_sku: dict[str, dict[str, list[str]]] = {}
        self._by_version: dict[str, dict[str, str]] = {}
        self._slug_owner: dict[str, dict[str, str]] = {}
        self._title_owner: dict[str, dict[str, str]] = {}

    def save(self, package: ProductContentPackage) -> ProductContentPackage:
        tenant = require_tenant_id(package.tenant_id)
        self._packages.setdefault(tenant, {})[package.package_id] = package
        sku_list = self._by_sku.setdefault(tenant, {}).setdefault(package.card.sku, [])
        if package.package_id not in sku_list:
            sku_list.append(package.package_id)
        self._by_version.setdefault(tenant, {})[package.version] = package.package_id
        self._slug_owner.setdefault(tenant, {})[package.seo.canonical_slug] = package.product_id
        self._title_owner.setdefault(tenant, {})[package.seo.seo_title] = package.product_id
        return package

    def get_by_version(self, *, tenant_id: str, version: str) -> ProductContentPackage | None:
        tenant = require_tenant_id(tenant_id)
        pid = self._by_version.get(tenant, {}).get(version)
        if not pid:
            return None
        return self._packages.get(tenant, {}).get(pid)

    def get(self, package_id: str, *, tenant_id: str) -> ProductContentPackage:
        tenant = require_tenant_id(tenant_id)
        bucket = self._packages.get(tenant, {})
        pkg = bucket.get(package_id)
        if pkg is None:
            # Cross-tenant: same id in another tenant must not leak
            for other, items in self._packages.items():
                if other != tenant and package_id in items:
                    raise ProductContentError(CONTENT_ACCESS_DENIED)
            raise ProductContentError(CONTENT_PACKAGE_NOT_FOUND)
        if pkg.tenant_id != tenant:
            raise ProductContentError(CONTENT_ACCESS_DENIED)
        return pkg

    def occupied_slugs(self, *, tenant_id: str, exclude_product_id: str | None = None) -> set[str]:
        tenant = require_tenant_id(tenant_id)
        return {
            slug
            for slug, pid in self._slug_owner.get(tenant, {}).items()
            if exclude_product_id is None or pid != exclude_product_id
        }

    def occupied_titles(self, *, tenant_id: str, exclude_product_id: str | None = None) -> set[str]:
        tenant = require_tenant_id(tenant_id)
        return {
            title
            for title, pid in self._title_owner.get(tenant, {}).items()
            if exclude_product_id is None or pid != exclude_product_id
        }

    def sku_ids(self, *, tenant_id: str, sku: str) -> list[str]:
        return list(self._by_sku.get(require_tenant_id(tenant_id), {}).get(sku, []))

    def other_product_ids_for_sku(self, *, tenant_id: str, sku: str, exclude_product_id: str) -> list[str]:
        out: list[str] = []
        for package_id in self.sku_ids(tenant_id=tenant_id, sku=sku):
            pkg = self._packages.get(require_tenant_id(tenant_id), {}).get(package_id)
            if pkg is not None and pkg.product_id != exclude_product_id:
                out.append(pkg.product_id)
        return out
