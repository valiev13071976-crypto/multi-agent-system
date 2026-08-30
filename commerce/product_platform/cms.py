"""Fake deterministic CMS/Bitrix provider for closure tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from commerce.product_platform.errors import COMMERCE_CMS_CONFLICT, ProductPlatformError


@dataclass
class FakeCmsState:
    external_id: str
    tenant_id: str
    product_id: str
    version_id: str
    price: Decimal
    stock: Decimal
    etag: int = 1


class FakeCommerceCmsProvider:
    provider_id = "fake-bitrix"

    def __init__(self):
        self._products: dict[tuple[str, str], FakeCmsState] = {}
        self._idempotency: dict[str, str] = {}
        self._price_writes: list[str] = []

    def create_product(
        self,
        *,
        tenant_id: str,
        product_id: str,
        version_id: str,
        idempotency_key: str,
    ) -> dict:
        if idempotency_key in self._idempotency:
            ext = self._idempotency[idempotency_key]
            return {"external_id": ext, "status": "created", "idempotent": True}
        external_id = f"cms-{uuid.uuid4().hex[:8]}"
        key = (tenant_id, external_id)
        if any(s.product_id == product_id for s in self._products.values() if s.tenant_id == tenant_id):
            pass  # allow updates via binding
        self._products[key] = FakeCmsState(
            external_id=external_id,
            tenant_id=tenant_id,
            product_id=product_id,
            version_id=version_id,
            price=Decimal("0"),
            stock=Decimal("0"),
        )
        self._idempotency[idempotency_key] = external_id
        return {"external_id": external_id, "status": "created", "verified": {"external_id": external_id}}

    def update_price(
        self,
        *,
        tenant_id: str,
        external_id: str,
        price: Decimal,
        currency: str,
        decision_id: str,
        idempotency_key: str,
        expected_etag: int | None = None,
    ) -> dict:
        if not decision_id:
            raise ProductPlatformError("COMMERCE_CMS_DENIED", "price update requires decision")
        if idempotency_key in self._idempotency:
            return {"external_id": external_id, "status": "updated", "idempotent": True}
        state = self._products.get((tenant_id, external_id))
        if state is None:
            raise ProductPlatformError("COMMERCE_NOT_FOUND")
        if expected_etag is not None and state.etag != expected_etag:
            raise ProductPlatformError(COMMERCE_CMS_CONFLICT)
        state.price = price
        state.etag += 1
        self._idempotency[idempotency_key] = external_id
        self._price_writes.append(decision_id)
        return {
            "external_id": external_id,
            "status": "updated",
            "verified": {"price": str(price), "currency": currency, "etag": state.etag},
        }

    def update_stock(
        self,
        *,
        tenant_id: str,
        external_id: str,
        stock: Decimal,
        idempotency_key: str,
        expected_etag: int | None = None,
    ) -> dict:
        if idempotency_key in self._idempotency:
            return {"external_id": external_id, "status": "updated", "idempotent": True}
        state = self._products.get((tenant_id, external_id))
        if state is None:
            raise ProductPlatformError("COMMERCE_NOT_FOUND")
        if expected_etag is not None and state.etag != expected_etag:
            raise ProductPlatformError(COMMERCE_CMS_CONFLICT)
        state.stock = stock
        state.etag += 1
        self._idempotency[idempotency_key] = external_id
        return {"external_id": external_id, "status": "updated", "verified": {"stock": str(stock), "etag": state.etag}}

    def update_product(
        self,
        *,
        tenant_id: str,
        external_id: str,
        version_id: str,
        idempotency_key: str,
        expected_etag: int | None = None,
    ) -> dict:
        if idempotency_key in self._idempotency:
            return {"external_id": external_id, "status": "updated", "idempotent": True}
        state = self._products.get((tenant_id, external_id))
        if state is None:
            raise ProductPlatformError("COMMERCE_NOT_FOUND")
        if expected_etag is not None and state.etag != expected_etag:
            raise ProductPlatformError(COMMERCE_CMS_CONFLICT)
        state.version_id = version_id
        state.etag += 1
        self._idempotency[idempotency_key] = external_id
        return {
            "external_id": external_id,
            "status": "updated",
            "verified": {"version_id": version_id, "etag": state.etag},
        }

    def archive_product(
        self,
        *,
        tenant_id: str,
        external_id: str,
        idempotency_key: str,
        expected_etag: int | None = None,
    ) -> dict:
        if idempotency_key in self._idempotency:
            return {"external_id": external_id, "status": "archived", "idempotent": True}
        state = self._products.get((tenant_id, external_id))
        if state is None:
            raise ProductPlatformError("COMMERCE_NOT_FOUND")
        if expected_etag is not None and state.etag != expected_etag:
            raise ProductPlatformError(COMMERCE_CMS_CONFLICT)
        state.etag += 1
        self._idempotency[idempotency_key] = external_id
        return {"external_id": external_id, "status": "archived", "verified": {"etag": state.etag}}

    def get_product(self, *, tenant_id: str, external_id: str) -> dict:
        state = self._products.get((tenant_id, external_id))
        if state is None:
            raise ProductPlatformError("COMMERCE_NOT_FOUND")
        return {
            "external_id": external_id,
            "product_id": state.product_id,
            "version_id": state.version_id,
            "price": str(state.price),
            "stock": str(state.stock),
            "etag": state.etag,
        }
