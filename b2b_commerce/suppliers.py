"""Supplier domain helpers."""

from __future__ import annotations

import uuid

from b2b_commerce.errors import B2B_SUPPLIER_DISABLED, B2B_SUPPLIER_NOT_FOUND, B2BCommerceError
from b2b_commerce.platform_models import Supplier


def new_supplier_id() -> str:
    return f"sup_{uuid.uuid4().hex[:12]}"


def require_active_supplier(supplier: Supplier | None) -> Supplier:
    if supplier is None:
        raise B2BCommerceError(B2B_SUPPLIER_NOT_FOUND)
    if supplier.status != "ACTIVE":
        raise B2BCommerceError(B2B_SUPPLIER_DISABLED)
    return supplier


def require_source_binding(supplier: Supplier, source_key: str) -> None:
    if supplier.source_bindings and source_key not in supplier.source_bindings:
        from b2b_commerce.errors import B2B_SOURCE_DENIED

        raise B2BCommerceError(B2B_SOURCE_DENIED)
