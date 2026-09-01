"""Tenant-scoped Ozon fixture catalog."""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal
from typing import Any


def _seed() -> dict[str, dict[str, dict]]:
    base = {
        "oz-card-1001": {
            "product_id": 701001,
            "offer_id": "OFFER-100",
            "seller_article": "OZ-SKU-100",
            "sku": "OZ-SKU-100",
            "barcode": "4600000007001",
            "title": "Ozon Smartphone Case Pro",
            "brand": "Acme",
            "category_id": "oz-cat-phones",
            "status": "PUBLISHED",
            "moderation_state": "APPROVED",
            "fulfillment": "FBS",
            "purchase_cost": "800.00",
            "seller_price": "1990.00",
            "old_price": "2490.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "attributes": {"color": "black", "material": "silicone"},
            "stock": {"wh-fbs-a-main": 20, "wh-fbs-a-east": 5},
        },
        "oz-card-1002": {
            "product_id": 701002,
            "offer_id": "OFFER-200",
            "seller_article": "OZ-SKU-200",
            "sku": "OZ-SKU-200",
            "barcode": "4600000007002",
            "title": "Ozon USB Cable",
            "brand": "Acme",
            "category_id": "oz-cat-accessories",
            "status": "PUBLISHED",
            "moderation_state": "APPROVED",
            "fulfillment": "FBS",
            "purchase_cost": "150.00",
            "seller_price": "590.00",
            "old_price": "690.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": {
                "ownership": "PLATFORM_CONTROLLED",
                "customer_visible_price": "490.00",
                "seller_price": "590.00",
            },
            "attributes": {"length": "1m"},
            "stock": {"wh-fbs-a-main": 100},
        },
        "oz-card-fbo": {
            "product_id": 701003,
            "offer_id": "OFFER-FBO",
            "seller_article": "OZ-SKU-FBO",
            "sku": "OZ-SKU-FBO",
            "barcode": "4600000007003",
            "title": "Ozon FBO Item",
            "brand": "Acme",
            "category_id": "oz-cat-accessories",
            "status": "PUBLISHED",
            "moderation_state": "APPROVED",
            "fulfillment": "FBO",
            "purchase_cost": "300.00",
            "seller_price": "1290.00",
            "old_price": "1490.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "attributes": {},
            "stock": {"wh-fbo-a-main": 50},
        },
        "oz-card-amb": {
            "product_id": 701999,
            "offer_id": "OFFER-AMB",
            "seller_article": "OZ-SKU-AMB",
            "sku": "OZ-SKU-AMB",
            "barcode": "4600000999999",
            "title": "Generic Ozon Gadget",
            "brand": "Acme",
            "category_id": "oz-cat-accessories",
            "status": "PUBLISHED",
            "moderation_state": "APPROVED",
            "fulfillment": "FBS",
            "purchase_cost": "200.00",
            "seller_price": "990.00",
            "old_price": "1090.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "attributes": {},
            "stock": {"wh-fbs-a-main": 1},
            "ambiguous_duplicate_name": True,
        },
    }
    tenant_b = copy.deepcopy(base)
    for card in tenant_b.values():
        card["product_id"] = int(card["product_id"]) + 900000
        card["offer_id"] = f"B-{card['offer_id']}"
        card["seller_article"] = card["seller_article"].replace("OZ-", "OZ-B-")
        card["sku"] = card["seller_article"]
        card["stock"] = {"wh-fbs-b-main": 7}
    return {
        "tenant-a": copy.deepcopy(base),
        "tenant-b": tenant_b,
        "default": copy.deepcopy(base),
    }


WAREHOUSE_MAP = {
    "tenant-a": {"fbs_main": "wh-fbs-a-main", "fbs_east": "wh-fbs-a-east", "fbo_main": "wh-fbo-a-main"},
    "tenant-b": {"fbs_main": "wh-fbs-b-main", "fbo_main": "wh-fbo-b-main"},
    "default": {"fbs_main": "wh-fbs-a-main", "fbs_east": "wh-fbs-a-east", "fbo_main": "wh-fbo-a-main"},
}


FULFILLMENT_WAREHOUSES = {
    "FBS": {"fbs_main", "fbs_east"},
    "FBO": {"fbo_main"},
}


def normalize_card(raw: dict) -> dict:
    return {
        "product_id": raw.get("product_id"),
        "offer_id": raw.get("offer_id") or "",
        "seller_article": raw.get("seller_article") or "",
        "sku": raw.get("sku") or raw.get("seller_article") or "",
        "barcode": raw.get("barcode") or "",
        "title": raw.get("title") or "",
        "brand": raw.get("brand") or "",
        "category_id": raw.get("category_id") or "",
        "status": raw.get("status") or "",
        "moderation_state": raw.get("moderation_state") or "",
        "fulfillment": raw.get("fulfillment") or "",
        "seller_price": str(raw.get("seller_price") or "0"),
        "old_price": str(raw.get("old_price") or "0"),
        "seller_discount_pct": str(raw.get("seller_discount_pct") or "0"),
        "currency": raw.get("currency") or "RUB",
        "platform_promo": dict(raw.get("platform_promo") or {}) if raw.get("platform_promo") else None,
        "attributes": dict(raw.get("attributes") or {}),
        "stock": dict(raw.get("stock") or {}),
        "purchase_cost": str(raw.get("purchase_cost") or "0"),
        "mode": "FIXTURE",
        "live": False,
    }


class OzonCatalogStore:
    def __init__(self):
        self._catalogs = _seed()
        self._mappings: dict[tuple[str, str], str] = {}
        self._write_counts: dict[str, int] = {}
        self._import_tasks: dict[str, dict] = {}

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
        product_id: int | str = "",
        offer_id: str = "",
        seller_article: str = "",
        sku: str = "",
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
            if product_id and str(card.get("product_id")) == str(product_id):
                matches.append(card)
            elif offer_id and card.get("offer_id") == offer_id:
                matches.append(card)
            elif seller_article and card.get("seller_article") == seller_article:
                matches.append(card)
            elif sku and card.get("sku") == sku:
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

    def resolve_target(self, *, tenant_id: str, seller_article: str) -> tuple[dict, dict]:
        cat = self.catalog(tenant_id)
        for card in cat.values():
            if card.get("seller_article") == seller_article or card.get("sku") == seller_article:
                return card, {}
        return {}, {}

    def read_price(self, *, tenant_id: str, seller_article: str) -> dict:
        card, _ = self.resolve_target(tenant_id=tenant_id, seller_article=seller_article)
        if not card:
            return {}
        base = Decimal(str(card.get("seller_price") or "0"))
        old = Decimal(str(card.get("old_price") or "0"))
        disc = Decimal(str(card.get("seller_discount_pct") or "0"))
        seller_effective = base * (Decimal("1") - disc / Decimal("100"))
        promo = card.get("platform_promo")
        customer_visible = (
            Decimal(str(promo.get("customer_visible_price")))
            if promo and promo.get("customer_visible_price")
            else seller_effective
        )
        return {
            "product_id": card.get("product_id"),
            "offer_id": card.get("offer_id"),
            "seller_article": seller_article,
            "sku": card.get("sku"),
            "seller_price": str(base),
            "old_price": str(old),
            "seller_discount_pct": str(disc),
            "seller_effective_price": str(seller_effective.quantize(Decimal("0.01"))),
            "customer_visible_price": str(customer_visible.quantize(Decimal("0.01"))),
            "currency": card.get("currency") or "RUB",
            "platform_promo": dict(promo) if promo else None,
            "promo_ownership": (promo or {}).get("ownership"),
            "seller_price_control": "SELLER_CONTROLLED",
            "customer_price_control": "PLATFORM_CONTROLLED" if promo else "DERIVED",
            "mode": "FIXTURE",
            "live": False,
        }

    def read_stock(self, *, tenant_id: str, seller_article: str, warehouse: str = "fbs_main") -> dict:
        card, _ = self.resolve_target(tenant_id=tenant_id, seller_article=seller_article)
        if not card:
            return {}
        wh_id = self.warehouse_id(tenant_id, warehouse)
        stock_map = card.get("stock") or {}
        qty = int(stock_map.get(wh_id) or 0)
        return {
            "product_id": card.get("product_id"),
            "offer_id": card.get("offer_id"),
            "seller_article": seller_article,
            "warehouse": warehouse,
            "warehouse_id": wh_id,
            "available": qty,
            "fulfillment": card.get("fulfillment"),
            "mode": "FIXTURE",
            "live": False,
        }

    def set_price(self, *, tenant_id: str, seller_article: str, new_amount: str) -> tuple[dict, dict, dict]:
        card, _ = self.resolve_target(tenant_id=tenant_id, seller_article=seller_article)
        if not card:
            return {}, {}, {}
        old = self.read_price(tenant_id=tenant_id, seller_article=seller_article)
        card["seller_price"] = new_amount
        card["seller_discount_pct"] = "0"
        new = self.read_price(tenant_id=tenant_id, seller_article=seller_article)
        return normalize_card(card), old, new

    def set_stock(self, *, tenant_id: str, seller_article: str, warehouse: str, quantity: int) -> tuple[dict, int]:
        card, _ = self.resolve_target(tenant_id=tenant_id, seller_article=seller_article)
        if not card:
            return {}, 0
        wh_id = self.warehouse_id(tenant_id, warehouse)
        stock_map = card.setdefault("stock", {})
        old = int(stock_map.get(wh_id) or 0)
        stock_map[wh_id] = quantity
        return normalize_card(card), old

    def create_import_task(
        self,
        *,
        tenant_id: str,
        payload: dict,
        panda_product_id: str = "",
        initial_status: str = "SUBMITTED",
    ) -> dict:
        task_id = f"oz-import-{uuid.uuid4().hex[:8]}"
        task = {
            "task_id": task_id,
            "tenant_id": tenant_id,
            "status": initial_status,
            "offer_id": str(payload.get("offer_id") or payload.get("seller_article") or ""),
            "product_id": None,
            "payload_summary": {"title": payload.get("title"), "seller_article": payload.get("seller_article")},
            "panda_product_id": panda_product_id,
            "mode": "FIXTURE",
            "live": False,
        }
        self._import_tasks[task_id] = task
        return task

    def get_import_task(self, task_id: str) -> dict | None:
        return self._import_tasks.get(task_id)

    def advance_import_task(self, task_id: str, *, status: str, product_id: int | None = None) -> dict:
        task = self._import_tasks.get(task_id)
        if not task:
            return {}
        task["status"] = status
        if product_id is not None:
            task["product_id"] = product_id
        return dict(task)

    def finalize_import_success(self, *, tenant_id: str, task_id: str) -> dict:
        task = self._import_tasks.get(task_id)
        if not task:
            return {}
        payload = dict(task.get("payload_summary") or {})
        card = self.create_card(
            tenant_id=tenant_id,
            payload={
                "offer_id": task.get("offer_id"),
                "seller_article": payload.get("seller_article") or task.get("offer_id"),
                "title": payload.get("title") or "Imported",
                "purchase_cost": "500",
            },
            panda_product_id=str(task.get("panda_product_id") or ""),
        )
        self.advance_import_task(task_id, status="SUCCEEDED", product_id=card.get("product_id"))
        return card

    def create_card(self, *, tenant_id: str, payload: dict, panda_product_id: str = "") -> dict:
        cat = self.catalog(tenant_id)
        cid = f"oz-card-{uuid.uuid4().hex[:6]}"
        card = {
            "product_id": int(payload.get("product_id") or 9000000 + len(cat)),
            "offer_id": str(payload.get("offer_id") or f"OFFER-{uuid.uuid4().hex[:4]}"),
            "seller_article": str(payload.get("seller_article") or payload.get("sku") or ""),
            "sku": str(payload.get("sku") or payload.get("seller_article") or ""),
            "barcode": str(payload.get("barcode") or ""),
            "title": str(payload.get("title") or payload.get("name") or ""),
            "brand": str(payload.get("brand") or ""),
            "category_id": str(payload.get("category_id") or ""),
            "status": "DRAFT",
            "moderation_state": "PENDING",
            "fulfillment": str(payload.get("fulfillment") or "FBS"),
            "purchase_cost": str(payload.get("purchase_cost") or "0"),
            "seller_price": str(payload.get("seller_price") or payload.get("price") or "0"),
            "old_price": "0",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "attributes": dict(payload.get("attributes") or {}),
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
                "posting_number": f"oz-posting-{page}-{i}",
                "order_id": f"oz-order-{page}-{i}",
                "offer_id": "OFFER-100",
                "seller_article": "OZ-SKU-100",
                "status": "awaiting_packaging",
                "fulfillment": "FBS",
                "warehouse_id": "wh-fbs-a-main",
                "quantity": 1,
                "total": "1990.00",
                "currency": "RUB",
            }
            for i in range(2)
        ]
        return {"items": items, "page": page, "next_page": page + 1 if page < 5 else None, "bounded": True, "mode": "FIXTURE", "live": False}

    def promotions_page(self, *, tenant_id: str) -> dict:
        return {
            "items": [
                {
                    "promotion_id": "oz-promo-1",
                    "title": "Ozon Platform Sale",
                    "status": "ACTIVE",
                    "seller_participation": "UNKNOWN",
                    "ownership": "PLATFORM_CONTROLLED",
                }
            ],
            "mode": "FIXTURE",
            "live": False,
        }

    def record_write(self, key: str) -> int:
        self._write_counts[key] = self._write_counts.get(key, 0) + 1
        return self._write_counts[key]

    def write_count(self, key: str) -> int:
        return self._write_counts.get(key, 0)

    def get_mapping(self, *, tenant_id: str, panda_product_id: str) -> str | None:
        return self._mappings.get((tenant_id, panda_product_id))


GLOBAL_OZON_CATALOG = OzonCatalogStore()
