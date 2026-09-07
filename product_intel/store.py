"""Tenant-scoped Product Catalog / Dataset store (spec section 17).

In-memory, dict-based persistence -- the same convention already used by
``data_intel.store.InMemoryDatasetStore``/``acquisition.store``. No new
database technology is introduced for Block 5.5.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from product_intel.platform_models import Product
from security.tenant import require_tenant_id, tenants_match


class ProductCatalogStore(ABC):
    @abstractmethod
    def save_product(self, product: Product) -> Product:
        raise NotImplementedError

    @abstractmethod
    def get_product(self, product_id: str, *, tenant_id: str) -> Product | None:
        raise NotImplementedError

    @abstractmethod
    def get_by_sku(self, sku: str, *, tenant_id: str) -> Product | None:
        raise NotImplementedError

    @abstractmethod
    def get_by_barcode(self, gtin: str, *, tenant_id: str) -> Product | None:
        raise NotImplementedError

    @abstractmethod
    def list_products(self, *, tenant_id: str, catalog_id: str | None = None) -> list[Product]:
        raise NotImplementedError

    @abstractmethod
    def delete_product(self, product_id: str, *, tenant_id: str) -> None:
        raise NotImplementedError


class InMemoryProductCatalogStore(ProductCatalogStore):
    def __init__(self):
        self._products: dict[str, Product] = {}
        self._catalog_members: dict[str, set[str]] = {}
        self._product_catalog: dict[str, str] = {}

    def save_product(self, product: Product, *, catalog_id: str | None = None) -> Product:
        require_tenant_id(product.tenant_id)
        self._products[product.product_id] = product
        cid = catalog_id or self._product_catalog.get(product.product_id) or "default"
        self._product_catalog[product.product_id] = cid
        self._catalog_members.setdefault(cid, set()).add(product.product_id)
        return product

    def get_product(self, product_id: str, *, tenant_id: str) -> Product | None:
        product = self._products.get(product_id)
        if product is None:
            return None
        if not tenants_match(tenant_id, product.tenant_id):
            return None
        return product

    def get_by_sku(self, sku: str, *, tenant_id: str) -> Product | None:
        if not sku:
            return None
        for product in self._products.values():
            if product.sku == sku and tenants_match(tenant_id, product.tenant_id):
                return product
        return None

    def get_by_barcode(self, gtin: str, *, tenant_id: str) -> Product | None:
        if not gtin:
            return None
        for product in self._products.values():
            if product.gtin == gtin and tenants_match(tenant_id, product.tenant_id):
                return product
        return None

    def list_products(self, *, tenant_id: str, catalog_id: str | None = None) -> list[Product]:
        ids = self._catalog_members.get(catalog_id) if catalog_id else None
        out = []
        for pid, product in self._products.items():
            if not tenants_match(tenant_id, product.tenant_id):
                continue
            if ids is not None and pid not in ids:
                continue
            out.append(product)
        return out

    def delete_product(self, product_id: str, *, tenant_id: str) -> None:
        product = self._products.get(product_id)
        if product is None or not tenants_match(tenant_id, product.tenant_id):
            return
        self._products.pop(product_id, None)
        cid = self._product_catalog.pop(product_id, None)
        if cid is not None:
            self._catalog_members.get(cid, set()).discard(product_id)
