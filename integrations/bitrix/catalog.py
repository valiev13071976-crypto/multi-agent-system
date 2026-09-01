"""Tenant-scoped Bitrix product catalog fixture + normalization."""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal
from typing import Any


def _seed_catalog() -> dict[str, dict[str, Any]]:
    """Deterministic seed products keyed by tenant."""
    base = {
        "bitrix-prod-1001": {
            "external_product_id": "bitrix-prod-1001",
            "xml_id": "PANDA-X100",
            "name": "Samsung Galaxy S24",
            "article": "SKU-X100",
            "section": "Smartphones",
            "active": True,
            "prices": [{"amount": "49990.00", "currency": "RUB", "price_type": "RETAIL"}],
            "stock": {"total": 10, "warehouses": {"main": 10}, "available": True},
            "offers": {
                "offer-1001-black": {
                    "offer_id": "offer-1001-black",
                    "article": "SKU-X100-BLK",
                    "variant": {"color": "black"},
                    "price": {"amount": "49990.00", "currency": "RUB", "price_type": "RETAIL"},
                    "stock": {"total": 5},
                },
                "offer-1001-white": {
                    "offer_id": "offer-1001-white",
                    "article": "SKU-X100-WHT",
                    "variant": {"color": "white"},
                    "price": {"amount": "51990.00", "currency": "RUB", "price_type": "RETAIL"},
                    "stock": {"total": 5},
                },
            },
            "properties": {"brand": "Samsung"},
            "version_hint": "v1",
        },
        "bitrix-prod-1002": {
            "external_product_id": "bitrix-prod-1002",
            "xml_id": "PANDA-X200",
            "name": "Samsung Phone Case",
            "article": "SKU-X200",
            "section": "Accessories",
            "active": False,
            "prices": [{"amount": "990.00", "currency": "RUB", "price_type": "RETAIL"}],
            "stock": {"total": 50, "warehouses": {"main": 50}, "available": True},
            "offers": {},
            "properties": {"brand": "Samsung"},
            "version_hint": "v1",
        },
        "bitrix-prod-1003": {
            "external_product_id": "bitrix-prod-1003",
            "xml_id": "PANDA-AMB",
            "name": "Samsung Accessory",
            "article": "SKU-AMBIG",
            "section": "Accessories",
            "active": True,
            "prices": [{"amount": "1500.00", "currency": "RUB", "price_type": "RETAIL"}],
            "stock": {"total": 3, "warehouses": {"main": 3}, "available": True},
            "offers": {},
            "properties": {"brand": "Samsung"},
            "version_hint": "v1",
            "ambiguous_duplicate_name": True,
        },
    }
    return {tid: copy.deepcopy(base) for tid in ("tenant-a", "tenant-b", "default")}


def normalize_product(raw: dict) -> dict:
    return {
        "external_product_id": raw.get("external_product_id") or raw.get("bitrix_id") or "",
        "xml_id": raw.get("xml_id") or "",
        "name": raw.get("name") or "",
        "article": raw.get("article") or raw.get("sku") or "",
        "section": raw.get("section") or "",
        "active": bool(raw.get("active")),
        "prices": list(raw.get("prices") or []),
        "stock": dict(raw.get("stock") or {}),
        "offers": {
            oid: {
                "offer_id": o.get("offer_id"),
                "article": o.get("article"),
                "variant": dict(o.get("variant") or {}),
                "price": dict(o.get("price") or {}),
                "stock": dict(o.get("stock") or {}),
            }
            for oid, o in (raw.get("offers") or {}).items()
        },
        "properties": dict(raw.get("properties") or {}),
        "version_hint": raw.get("version_hint") or "",
        "mode": "FIXTURE",
        "live": False,
    }


class BitrixCatalogStore:
    """In-memory tenant-scoped catalog — correctness-critical mapping persisted per tenant."""

    def __init__(self):
        self._catalogs: dict[str, dict[str, dict]] = _seed_catalog()
        self._mappings: dict[tuple[str, str], str] = {}  # (tenant, panda_id) -> bitrix_id
        self._write_counts: dict[str, int] = {}

    def catalog(self, tenant_id: str) -> dict[str, dict]:
        tid = tenant_id or "default"
        if tid not in self._catalogs:
            self._catalogs[tid] = copy.deepcopy(self._catalogs["default"])
        return self._catalogs[tid]

    def list_products(self, *, tenant_id: str, page: int = 1, page_size: int = 2) -> dict:
        cat = self.catalog(tenant_id)
        items = [normalize_product(v) for v in cat.values()]
        start = (page - 1) * page_size
        chunk = items[start : start + page_size]
        next_page = page + 1 if start + page_size < len(items) else None
        return {
            "items": chunk,
            "next_page": next_page,
            "page": page,
            "bounded": True,
            "mode": "FIXTURE",
            "live": False,
        }

    def lookup(
        self,
        *,
        tenant_id: str,
        bitrix_id: str = "",
        xml_id: str = "",
        article: str = "",
        panda_product_id: str = "",
        name: str = "",
        allow_name_only: bool = False,
    ) -> dict | list[dict]:
        cat = self.catalog(tenant_id)
        if panda_product_id:
            mapped = self._mappings.get((tenant_id, panda_product_id))
            if mapped and mapped in cat:
                return normalize_product(cat[mapped])

        matches = []
        for prod in cat.values():
            if bitrix_id and prod.get("external_product_id") == bitrix_id:
                matches.append(prod)
            elif xml_id and prod.get("xml_id") == xml_id:
                matches.append(prod)
            elif article and (
                prod.get("article") == article
                or any(o.get("article") == article for o in (prod.get("offers") or {}).values())
            ):
                matches.append(prod)
            elif name and prod.get("name", "").casefold() == name.casefold():
                matches.append(prod)

        if not matches:
            return {}
        if len(matches) > 1 and not allow_name_only:
            return matches  # ambiguous
        if len(matches) > 1 and allow_name_only:
            amb = [m for m in matches if m.get("ambiguous_duplicate_name")]
            if len(amb) > 1 or len(matches) > 1:
                return matches
        return normalize_product(matches[0])

    def resolve_offer(self, *, tenant_id: str, article: str) -> tuple[dict, dict] | None:
        cat = self.catalog(tenant_id)
        for prod in cat.values():
            for oid, offer in (prod.get("offers") or {}).items():
                if offer.get("article") == article:
                    return prod, offer
            if prod.get("article") == article:
                return prod, {}
        return None

    def create_product(self, *, tenant_id: str, payload: dict, panda_product_id: str = "") -> dict:
        cat = self.catalog(tenant_id)
        bid = f"bitrix-prod-{uuid.uuid4().hex[:8]}"
        article = str(payload.get("article") or payload.get("sku") or payload.get("PROPERTY_ARTNUMBER") or "")
        prod = {
            "external_product_id": bid,
            "xml_id": str(payload.get("xml_id") or payload.get("XML_ID") or f"PANDA-{bid}"),
            "name": str(payload.get("name") or payload.get("NAME") or payload.get("title") or ""),
            "article": article,
            "section": str(payload.get("section") or "Uncategorized"),
            "active": bool(payload.get("active", False)),
            "prices": [
                {
                    "amount": str(payload.get("price") or payload.get("amount") or "0"),
                    "currency": str(payload.get("currency") or "RUB"),
                    "price_type": str(payload.get("price_type") or "RETAIL"),
                }
            ],
            "stock": {"total": int(payload.get("stock") or 0), "warehouses": {"main": int(payload.get("stock") or 0)}, "available": True},
            "offers": {},
            "properties": dict(payload.get("properties") or {}),
            "version_hint": "v1",
        }
        cat[bid] = prod
        if panda_product_id:
            self._mappings[(tenant_id, panda_product_id)] = bid
        return normalize_product(prod)

    def update_product(self, *, tenant_id: str, bitrix_id: str, changes: dict) -> dict:
        cat = self.catalog(tenant_id)
        if bitrix_id not in cat:
            return {}
        prod = cat[bitrix_id]
        for k, v in changes.items():
            if k in {"name", "article", "section", "active"}:
                prod[k] = v
            elif k == "properties":
                prod.setdefault("properties", {}).update(v)
        prod["version_hint"] = f"v{int(str(prod.get('version_hint','v1')).lstrip('v') or 1)+1}"
        return normalize_product(prod)

    def set_price(
        self,
        *,
        tenant_id: str,
        article: str,
        new_amount: str,
        currency: str = "RUB",
        price_type: str = "RETAIL",
    ) -> tuple[dict, dict, dict]:
        resolved = self.resolve_offer(tenant_id=tenant_id, article=article)
        if not resolved:
            return {}, {}, {}
        prod, offer = resolved
        old = {}
        if offer:
            old = dict(offer.get("price") or {})
            offer["price"] = {"amount": new_amount, "currency": currency, "price_type": price_type}
        else:
            old = dict((prod.get("prices") or [{}])[0])
            prod["prices"] = [{"amount": new_amount, "currency": currency, "price_type": price_type}]
        return normalize_product(prod), old, {"amount": new_amount, "currency": currency, "price_type": price_type}

    def set_stock(self, *, tenant_id: str, article: str, quantity: int) -> tuple[dict, int]:
        resolved = self.resolve_offer(tenant_id=tenant_id, article=article)
        if not resolved:
            return {}, 0
        prod, offer = resolved
        if offer:
            old = int((offer.get("stock") or {}).get("total") or 0)
            offer.setdefault("stock", {})["total"] = quantity
        else:
            old = int((prod.get("stock") or {}).get("total") or 0)
            prod.setdefault("stock", {})["total"] = quantity
            prod["stock"]["warehouses"] = {"main": quantity}
        return normalize_product(prod), old

    def publish(self, *, tenant_id: str, bitrix_id: str) -> dict:
        cat = self.catalog(tenant_id)
        if bitrix_id not in cat:
            return {}
        cat[bitrix_id]["active"] = True
        return normalize_product(cat[bitrix_id])

    def read_price(self, *, tenant_id: str, article: str) -> dict:
        resolved = self.resolve_offer(tenant_id=tenant_id, article=article)
        if not resolved:
            return {}
        prod, offer = resolved
        if offer:
            p = offer.get("price") or {}
            return {
                "article": article,
                "offer_id": offer.get("offer_id"),
                "product_id": prod.get("external_product_id"),
                "price": dict(p),
                "currency": p.get("currency", "RUB"),
                "price_type": p.get("price_type", "RETAIL"),
                "mode": "FIXTURE",
                "live": False,
            }
        p = (prod.get("prices") or [{}])[0]
        return {
            "article": article,
            "product_id": prod.get("external_product_id"),
            "price": dict(p),
            "currency": p.get("currency", "RUB"),
            "price_type": p.get("price_type", "RETAIL"),
            "mode": "FIXTURE",
            "live": False,
        }

    def read_stock(self, *, tenant_id: str, article: str) -> dict:
        resolved = self.resolve_offer(tenant_id=tenant_id, article=article)
        if not resolved:
            return {}
        prod, offer = resolved
        if offer:
            s = offer.get("stock") or {}
            return {
                "article": article,
                "offer_id": offer.get("offer_id"),
                "product_id": prod.get("external_product_id"),
                "total": int(s.get("total") or 0),
                "available": prod.get("active", True),
                "mode": "FIXTURE",
                "live": False,
            }
        s = prod.get("stock") or {}
        return {
            "article": article,
            "product_id": prod.get("external_product_id"),
            "total": int(s.get("total") or 0),
            "warehouses": dict(s.get("warehouses") or {}),
            "available": bool(s.get("available", True)),
            "mode": "FIXTURE",
            "live": False,
        }

    def record_write(self, key: str) -> int:
        self._write_counts[key] = self._write_counts.get(key, 0) + 1
        return self._write_counts[key]

    def write_count(self, key: str) -> int:
        return self._write_counts.get(key, 0)

    def bind_mapping(self, *, tenant_id: str, panda_product_id: str, bitrix_id: str) -> None:
        self._mappings[(tenant_id, panda_product_id)] = bitrix_id

    def get_mapping(self, *, tenant_id: str, panda_product_id: str) -> str | None:
        return self._mappings.get((tenant_id, panda_product_id))

    def orders_page(self, *, tenant_id: str, page: int = 1) -> dict:
        items = [
            {
                "order_id": f"bitrix-order-{page}-{i}",
                "status": "NEW",
                "total": str(Decimal("1000") * (i + 1)),
                "currency": "RUB",
                "items_summary": [{"article": "SKU-X100", "qty": 1}],
            }
            for i in range(2)
        ]
        return {"items": items, "page": page, "next_page": page + 1 if page < 5 else None, "bounded": True, "mode": "FIXTURE", "live": False}


# Shared fixture store for deterministic cross-request state (restart recovery tests clone this)
GLOBAL_BITRIX_CATALOG = BitrixCatalogStore()
