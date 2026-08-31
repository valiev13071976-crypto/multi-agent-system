"""Provider-neutral 1C adapter boundary + deterministic fixture."""

from __future__ import annotations

import uuid
from decimal import Decimal


class FakeOneCAdapter:
    """Deterministic fake — does not claim live 1C connectivity."""

    provider_id = "fake-1c"
    system = "1c"

    def __init__(self):
        self._nomenclature: dict[str, dict] = {}
        self._stocks: dict[str, Decimal] = {}
        self._prices: dict[str, Decimal] = {}
        self._orders: dict[str, dict] = {}
        self._idempotency: dict[str, str] = {}

    def capabilities(self) -> dict:
        return {
            "products": True,
            "prices": True,
            "stocks": True,
            "orders": True,
            "counterparties": True,
            "live": False,
            "fake": True,
        }

    def health(self) -> dict:
        return {"status": "healthy", "provider_id": self.provider_id, "authenticated": False}

    def pull_nomenclature(self) -> list[dict]:
        return list(self._nomenclature.values())

    def seed_product(self, *, external_id: str, sku: str, title: str, price: str = "100.00", stock: str = "10") -> dict:
        row = {
            "guid": external_id,
            "code": sku,
            "article": sku,
            "name": title,
            "price": price,
            "stock": stock,
            "warehouse_id": "main",
        }
        self._nomenclature[external_id] = row
        self._prices[external_id] = Decimal(price)
        self._stocks[external_id] = Decimal(stock)
        return row

    def push_order(self, *, order: dict, idempotency_key: str) -> dict:
        if idempotency_key in self._idempotency:
            return {"external_id": self._idempotency[idempotency_key], "idempotent": True, "status": "accepted"}
        ext = f"1c-order-{uuid.uuid4().hex[:8]}"
        self._idempotency[idempotency_key] = ext
        self._orders[ext] = dict(order)
        return {"external_id": ext, "idempotent": False, "status": "accepted", "fake": True}

    def normalize_product(self, row: dict) -> dict:
        return {
            "source": "1c",
            "external_id": str(row.get("guid") or ""),
            "sku": str(row.get("article") or row.get("code") or ""),
            "title": str(row.get("name") or ""),
            "price": str(row.get("price") or "0"),
            "stock": str(row.get("stock") or "0"),
            "location_id": str(row.get("warehouse_id") or "main"),
        }
