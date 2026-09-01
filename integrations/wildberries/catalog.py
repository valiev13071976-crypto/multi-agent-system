"""Tenant-scoped Wildberries fixture catalog."""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal
from typing import Any


def _seed() -> dict[str, dict[str, dict]]:
    base = {
        "wb-card-1001": {
            "nm_id": 1001001,
            "chrt_id": 2001001,
            "seller_article": "WB-SKU-100",
            "barcode": "4600000001001",
            "title": "Smartphone Case Pro",
            "brand": "Acme",
            "subject_id": "wb-cat-phones",
            "status": "PUBLISHED",
            "fulfillment": "FBS",
            "purchase_cost": "800.00",
            "base_price": "1990.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "variants": {
                "2001002": {
                    "chrt_id": 2001002,
                    "seller_article": "WB-SKU-100-BLK",
                    "barcode": "4600000001002",
                    "color": "black",
                    "base_price": "2190.00",
                    "seller_discount_pct": "5",
                    "stock": {"wh-fbs-main": 12, "wh-fbs-east": 3},
                }
            },
            "stock": {"wh-fbs-main": 20, "wh-fbs-east": 5},
        },
        "wb-card-1002": {
            "nm_id": 1001002,
            "chrt_id": 2002001,
            "seller_article": "WB-SKU-200",
            "barcode": "4600000002001",
            "title": "USB Cable",
            "brand": "Acme",
            "subject_id": "wb-cat-accessories",
            "status": "PUBLISHED",
            "fulfillment": "FBS",
            "purchase_cost": "150.00",
            "base_price": "590.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": {
                "ownership": "PLATFORM_CONTROLLED",
                "buyer_visible_price": "490.00",
                "seller_price": "590.00",
            },
            "variants": {},
            "stock": {"wh-fbs-main": 100},
        },
        "wb-card-amb": {
            "nm_id": 1001999,
            "chrt_id": 2009999,
            "seller_article": "WB-SKU-AMB",
            "barcode": "4600000999999",
            "title": "Generic Gadget",
            "brand": "Acme",
            "subject_id": "wb-cat-accessories",
            "status": "PUBLISHED",
            "fulfillment": "FBS",
            "purchase_cost": "200.00",
            "base_price": "990.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "variants": {},
            "stock": {"wh-fbs-main": 1},
            "ambiguous_duplicate_name": True,
        },
    }
    return {tid: copy.deepcopy(base) for tid in ("tenant-a", "tenant-b", "default")}


WAREHOUSE_MAP = {
    "tenant-a": {"main": "wh-fbs-main", "east": "wh-fbs-east"},
    "tenant-b": {"main": "wh-fbs-main"},
    "default": {"main": "wh-fbs-main", "east": "wh-fbs-east"},
}


def normalize_card(raw: dict) -> dict:
    return {
        "nm_id": raw.get("nm_id"),
        "chrt_id": raw.get("chrt_id"),
        "seller_article": raw.get("seller_article") or "",
        "barcode": raw.get("barcode") or "",
        "title": raw.get("title") or "",
        "brand": raw.get("brand") or "",
        "subject_id": raw.get("subject_id") or "",
        "status": raw.get("status") or "",
        "fulfillment": raw.get("fulfillment") or "",
        "base_price": str(raw.get("base_price") or "0"),
        "seller_discount_pct": str(raw.get("seller_discount_pct") or "0"),
        "currency": raw.get("currency") or "RUB",
        "platform_promo": dict(raw.get("platform_promo") or {}) if raw.get("platform_promo") else None,
        "variants": {
            str(k): {
                "chrt_id": v.get("chrt_id"),
                "seller_article": v.get("seller_article"),
                "barcode": v.get("barcode"),
                "base_price": str(v.get("base_price") or "0"),
                "seller_discount_pct": str(v.get("seller_discount_pct") or "0"),
                "stock": dict(v.get("stock") or {}),
            }
            for k, v in (raw.get("variants") or {}).items()
        },
        "stock": dict(raw.get("stock") or {}),
        "purchase_cost": str(raw.get("purchase_cost") or "0"),
        "mode": "FIXTURE",
        "live": False,
    }


class WildberriesCatalogStore:
    def __init__(self):
        self._catalogs = _seed()
        self._mappings: dict[tuple[str, str], str] = {}
        self._write_counts: dict[str, int] = {}

    def catalog(self, tenant_id: str) -> dict[str, dict]:
        tid = tenant_id or "default"
        if tid not in self._catalogs:
            self._catalogs[tid] = copy.deepcopy(self._catalogs["default"])
        return self._catalogs[tid]

    def warehouse_id(self, tenant_id: str, panda_wh: str) -> str:
        wh = WAREHOUSE_MAP.get(tenant_id or "default", {}).get(panda_wh)
        if not wh:
            raise KeyError("warehouse_not_mapped")
        return wh

    def lookup(
        self,
        *,
        tenant_id: str,
        nm_id: int | str = "",
        chrt_id: int | str = "",
        seller_article: str = "",
        barcode: str = "",
        panda_product_id: str = "",
        name: str = "",
    ) -> dict | list[dict]:
        cat = self.catalog(tenant_id)
        if panda_product_id:
            mapped = self._mappings.get((tenant_id, panda_product_id))
            if mapped and mapped in cat:
                return normalize_card(cat[mapped])
        matches = []
        for card in cat.values():
            if nm_id and str(card.get("nm_id")) == str(nm_id):
                matches.append(card)
            elif chrt_id and (
                str(card.get("chrt_id")) == str(chrt_id)
                or any(str(v.get("chrt_id")) == str(chrt_id) for v in (card.get("variants") or {}).values())
            ):
                matches.append(card)
            elif seller_article and (
                card.get("seller_article") == seller_article
                or any(v.get("seller_article") == seller_article for v in (card.get("variants") or {}).values())
            ):
                matches.append(card)
            elif barcode and card.get("barcode") == barcode:
                matches.append(card)
            elif name and card.get("title", "").casefold() == name.casefold():
                matches.append(card)
        if not matches:
            return {}
        if len(matches) > 1:
            return matches
        return normalize_card(matches[0])

    def resolve_variant(self, *, tenant_id: str, seller_article: str) -> tuple[dict, dict]:
        cat = self.catalog(tenant_id)
        for card in cat.values():
            for vid, var in (card.get("variants") or {}).items():
                if var.get("seller_article") == seller_article:
                    return card, var
            if card.get("seller_article") == seller_article:
                return card, {}
        return {}, {}

    def read_price(self, *, tenant_id: str, seller_article: str) -> dict:
        card, var = self.resolve_variant(tenant_id=tenant_id, seller_article=seller_article)
        if not card:
            return {}
        base = Decimal(str((var or {}).get("base_price") or card.get("base_price") or "0"))
        disc = Decimal(str((var or {}).get("seller_discount_pct") or card.get("seller_discount_pct") or "0"))
        seller_effective = base * (Decimal("1") - disc / Decimal("100"))
        promo = card.get("platform_promo")
        return {
            "seller_article": seller_article,
            "nm_id": card.get("nm_id"),
            "chrt_id": (var or {}).get("chrt_id") or card.get("chrt_id"),
            "base_price": str(base),
            "seller_discount_pct": str(disc),
            "seller_effective_price": str(seller_effective.quantize(Decimal("0.01"))),
            "currency": card.get("currency") or "RUB",
            "platform_promo": dict(promo) if promo else None,
            "buyer_visible_price": str(promo.get("buyer_visible_price")) if promo else str(seller_effective.quantize(Decimal("0.01"))),
            "promo_ownership": (promo or {}).get("ownership"),
            "mode": "FIXTURE",
            "live": False,
        }

    def read_stock(self, *, tenant_id: str, seller_article: str, warehouse: str = "main") -> dict:
        card, var = self.resolve_variant(tenant_id=tenant_id, seller_article=seller_article)
        if not card:
            return {}
        wh_id = self.warehouse_id(tenant_id, warehouse)
        stock_map = (var or {}).get("stock") or card.get("stock") or {}
        qty = int(stock_map.get(wh_id) or 0)
        return {
            "seller_article": seller_article,
            "nm_id": card.get("nm_id"),
            "chrt_id": (var or {}).get("chrt_id") or card.get("chrt_id"),
            "warehouse": warehouse,
            "warehouse_id": wh_id,
            "available": qty,
            "fulfillment": card.get("fulfillment"),
            "mode": "FIXTURE",
            "live": False,
        }

    def set_price(self, *, tenant_id: str, seller_article: str, new_amount: str) -> tuple[dict, dict, dict]:
        card, var = self.resolve_variant(tenant_id=tenant_id, seller_article=seller_article)
        if not card:
            return {}, {}, {}
        old = self.read_price(tenant_id=tenant_id, seller_article=seller_article)
        if var:
            var["base_price"] = new_amount
            var["seller_discount_pct"] = "0"
        else:
            card["base_price"] = new_amount
            card["seller_discount_pct"] = "0"
        new = self.read_price(tenant_id=tenant_id, seller_article=seller_article)
        return normalize_card(card), old, new

    def set_stock(self, *, tenant_id: str, seller_article: str, warehouse: str, quantity: int) -> tuple[dict, int]:
        card, var = self.resolve_variant(tenant_id=tenant_id, seller_article=seller_article)
        if not card:
            return {}, 0
        wh_id = self.warehouse_id(tenant_id, warehouse)
        stock_map = var.setdefault("stock", {}) if var else card.setdefault("stock", {})
        old = int(stock_map.get(wh_id) or 0)
        stock_map[wh_id] = quantity
        return normalize_card(card), old

    def create_card(self, *, tenant_id: str, payload: dict, panda_product_id: str = "") -> dict:
        cat = self.catalog(tenant_id)
        cid = f"wb-card-{uuid.uuid4().hex[:6]}"
        card = {
            "nm_id": int(payload.get("nm_id") or 9000000 + len(cat)),
            "chrt_id": int(payload.get("chrt_id") or 9100000 + len(cat)),
            "seller_article": str(payload.get("seller_article") or payload.get("sku") or ""),
            "barcode": str(payload.get("barcode") or ""),
            "title": str(payload.get("title") or payload.get("name") or ""),
            "brand": str(payload.get("brand") or ""),
            "subject_id": str(payload.get("subject_id") or payload.get("category_id") or ""),
            "status": "DRAFT",
            "fulfillment": str(payload.get("fulfillment") or "FBS"),
            "purchase_cost": str(payload.get("purchase_cost") or "0"),
            "base_price": str(payload.get("base_price") or payload.get("price") or "0"),
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "variants": {},
            "stock": {},
        }
        cat[cid] = card
        if panda_product_id:
            self._mappings[(tenant_id, panda_product_id)] = cid
        return normalize_card(card)

    def list_cards(self, *, tenant_id: str, page: int = 1, page_size: int = 2) -> dict:
        items = [normalize_card(v) for v in self.catalog(tenant_id).values()]
        start = (page - 1) * page_size
        chunk = items[start : start + page_size]
        next_page = page + 1 if start + page_size < len(items) else None
        return {"items": chunk, "next_page": next_page, "page": page, "bounded": True, "mode": "FIXTURE", "live": False}

    def orders_page(self, *, tenant_id: str, page: int = 1) -> dict:
        items = [
            {
                "order_id": f"wb-order-{page}-{i}",
                "seller_article": "WB-SKU-100",
                "status": "NEW",
                "fulfillment": "FBS",
                "warehouse_id": "wh-fbs-main",
                "quantity": 1,
                "total": "1990.00",
                "currency": "RUB",
            }
            for i in range(2)
        ]
        return {"items": items, "page": page, "next_page": page + 1 if page < 5 else None, "bounded": True, "mode": "FIXTURE", "live": False}

    def record_write(self, key: str) -> int:
        self._write_counts[key] = self._write_counts.get(key, 0) + 1
        return self._write_counts[key]

    def write_count(self, key: str) -> int:
        return self._write_counts.get(key, 0)


GLOBAL_WB_CATALOG = WildberriesCatalogStore()
