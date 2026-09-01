"""Tenant-scoped Yandex Market fixture catalog."""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal


def _seed() -> dict[str, dict[str, dict]]:
    base = {
        "ym-offer-1001": {
            "business_id": 100001,
            "campaign_id": "camp-a-001",
            "offer_id": "OFFER-YM-100",
            "shop_sku": "YM-SKU-100",
            "market_sku": "MKT-801001",
            "barcode": "4600000008001",
            "title": "Yandex Market Case Pro",
            "brand": "Acme",
            "category_id": "ym-cat-phones",
            "status": "PUBLISHED",
            "moderation_state": "APPROVED",
            "fulfillment": "DBS",
            "purchase_cost": "750.00",
            "seller_price": "1890.00",
            "old_price": "2290.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "attributes": {"color": "black"},
            "stock": {"wh-dbs-a-main": 18, "wh-dbs-a-east": 4},
        },
        "ym-offer-1002": {
            "business_id": 100001,
            "campaign_id": "camp-a-001",
            "offer_id": "OFFER-YM-200",
            "shop_sku": "YM-SKU-200",
            "market_sku": "MKT-801002",
            "barcode": "4600000008002",
            "title": "Yandex Market USB Cable",
            "brand": "Acme",
            "category_id": "ym-cat-accessories",
            "status": "PUBLISHED",
            "moderation_state": "APPROVED",
            "fulfillment": "DBS",
            "purchase_cost": "140.00",
            "seller_price": "580.00",
            "old_price": "680.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": {
                "ownership": "PLATFORM_CONTROLLED",
                "customer_visible_price": "480.00",
                "seller_price": "580.00",
            },
            "attributes": {"length": "1m"},
            "stock": {"wh-dbs-a-main": 90},
        },
        "ym-offer-fby": {
            "business_id": 100001,
            "campaign_id": "camp-a-001",
            "offer_id": "OFFER-YM-FBY",
            "shop_sku": "YM-SKU-FBY",
            "market_sku": "MKT-801003",
            "barcode": "4600000008003",
            "title": "Yandex Market FBY Item",
            "brand": "Acme",
            "category_id": "ym-cat-accessories",
            "status": "PUBLISHED",
            "moderation_state": "APPROVED",
            "fulfillment": "FBY",
            "purchase_cost": "280.00",
            "seller_price": "1190.00",
            "old_price": "1390.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "attributes": {},
            "stock": {"wh-fby-a-main": 45},
        },
        "ym-offer-amb": {
            "business_id": 100001,
            "campaign_id": "camp-a-001",
            "offer_id": "OFFER-YM-AMB",
            "shop_sku": "YM-SKU-AMB",
            "market_sku": "MKT-801999",
            "barcode": "4600000999998",
            "title": "Generic Yandex Gadget",
            "brand": "Acme",
            "category_id": "ym-cat-accessories",
            "status": "PUBLISHED",
            "moderation_state": "APPROVED",
            "fulfillment": "DBS",
            "purchase_cost": "190.00",
            "seller_price": "950.00",
            "old_price": "1050.00",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "attributes": {},
            "stock": {"wh-dbs-a-main": 2},
            "ambiguous_duplicate_name": True,
        },
    }
    tenant_b = copy.deepcopy(base)
    for card in tenant_b.values():
        card["business_id"] = 200002
        card["campaign_id"] = "camp-b-001"
        card["offer_id"] = f"B-{card['offer_id']}"
        card["shop_sku"] = card["shop_sku"].replace("YM-", "YM-B-")
        card["market_sku"] = f"MKT-B-{card['market_sku'].split('-')[-1]}"
        card["stock"] = {"wh-dbs-b-main": 6}
    return {
        "tenant-a": copy.deepcopy(base),
        "tenant-b": tenant_b,
        "default": copy.deepcopy(base),
    }


BUSINESS_MAP = {
    "tenant-a": {"business_id": 100001, "default_campaign": "camp-a-001"},
    "tenant-b": {"business_id": 200002, "default_campaign": "camp-b-001"},
    "default": {"business_id": 100001, "default_campaign": "camp-a-001"},
}


WAREHOUSE_MAP = {
    "tenant-a": {"dbs_main": "wh-dbs-a-main", "dbs_east": "wh-dbs-a-east", "fby_main": "wh-fby-a-main"},
    "tenant-b": {"dbs_main": "wh-dbs-b-main", "fby_main": "wh-fby-b-main"},
    "default": {"dbs_main": "wh-dbs-a-main", "dbs_east": "wh-dbs-a-east", "fby_main": "wh-fby-a-main"},
}


FULFILLMENT_WAREHOUSES = {
    "DBS": {"dbs_main", "dbs_east"},
    "FBY": {"fby_main"},
}


def normalize_offer(raw: dict) -> dict:
    return {
        "business_id": raw.get("business_id"),
        "campaign_id": raw.get("campaign_id") or "",
        "offer_id": raw.get("offer_id") or "",
        "shop_sku": raw.get("shop_sku") or "",
        "market_sku": raw.get("market_sku") or "",
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


class YandexMarketCatalogStore:
    def __init__(self):
        self._catalogs = _seed()
        self._mappings: dict[tuple[str, str], str] = {}
        self._write_counts: dict[str, int] = {}
        self._submission_tasks: dict[str, dict] = {}

    def catalog(self, tenant_id: str) -> dict[str, dict]:
        tid = tenant_id or "default"
        if tid not in self._catalogs:
            self._catalogs[tid] = copy.deepcopy(self._catalogs["default"])
        return self._catalogs[tid]

    def business_scope(self, tenant_id: str) -> dict:
        return dict(BUSINESS_MAP.get(tenant_id or "default", BUSINESS_MAP["default"]))

    def warehouse_id(self, tenant_id: str, panda_wh: str) -> str:
        wh = WAREHOUSE_MAP.get(tenant_id or "default", {}).get(panda_wh)
        if not wh:
            raise KeyError("warehouse_not_mapped")
        return wh

    def lookup(
        self,
        *,
        tenant_id: str,
        business_id: int | str = "",
        campaign_id: str = "",
        offer_id: str = "",
        shop_sku: str = "",
        market_sku: str = "",
        barcode: str = "",
        panda_product_id: str = "",
        name: str = "",
    ) -> dict | list[dict]:
        cat = self.catalog(tenant_id)
        if panda_product_id:
            mapped = self._mappings.get((tenant_id, panda_product_id))
            if mapped and mapped in cat:
                return normalize_offer(cat[mapped])
        matches = []
        for offer in cat.values():
            if business_id and str(offer.get("business_id")) != str(business_id):
                continue
            if campaign_id and offer.get("campaign_id") != campaign_id:
                continue
            if offer_id and offer.get("offer_id") == offer_id:
                matches.append(offer)
            elif shop_sku and offer.get("shop_sku") == shop_sku:
                matches.append(offer)
            elif market_sku and offer.get("market_sku") == market_sku:
                matches.append(offer)
            elif barcode and offer.get("barcode") == barcode:
                matches.append(offer)
            elif name and offer.get("title", "").casefold() == name.casefold():
                matches.append(offer)
        if not matches:
            return {}
        if len(matches) > 1:
            return matches
        return normalize_offer(matches[0])

    def resolve_target(self, *, tenant_id: str, shop_sku: str, campaign_id: str = "") -> tuple[dict, dict]:
        scope = self.business_scope(tenant_id)
        cid = campaign_id or scope["default_campaign"]
        for offer in self.catalog(tenant_id).values():
            if offer.get("shop_sku") == shop_sku and offer.get("campaign_id") == cid:
                return offer, {}
        return {}, {}

    def read_price(self, *, tenant_id: str, shop_sku: str, campaign_id: str = "") -> dict:
        offer, _ = self.resolve_target(tenant_id=tenant_id, shop_sku=shop_sku, campaign_id=campaign_id)
        if not offer:
            return {}
        base = Decimal(str(offer.get("seller_price") or "0"))
        old = Decimal(str(offer.get("old_price") or "0"))
        disc = Decimal(str(offer.get("seller_discount_pct") or "0"))
        seller_effective = base * (Decimal("1") - disc / Decimal("100"))
        promo = offer.get("platform_promo")
        customer_visible = (
            Decimal(str(promo.get("customer_visible_price")))
            if promo and promo.get("customer_visible_price")
            else seller_effective
        )
        return {
            "business_id": offer.get("business_id"),
            "campaign_id": offer.get("campaign_id"),
            "offer_id": offer.get("offer_id"),
            "shop_sku": shop_sku,
            "market_sku": offer.get("market_sku"),
            "seller_price": str(base),
            "old_price": str(old),
            "seller_discount_pct": str(disc),
            "seller_effective_price": str(seller_effective.quantize(Decimal("0.01"))),
            "customer_visible_price": str(customer_visible.quantize(Decimal("0.01"))),
            "currency": offer.get("currency") or "RUB",
            "platform_promo": dict(promo) if promo else None,
            "promo_ownership": (promo or {}).get("ownership"),
            "seller_price_control": "SELLER_CONTROLLED",
            "customer_price_control": "PLATFORM_CONTROLLED" if promo else "DERIVED",
            "mode": "FIXTURE",
            "live": False,
        }

    def read_stock(self, *, tenant_id: str, shop_sku: str, warehouse: str = "dbs_main", campaign_id: str = "") -> dict:
        offer, _ = self.resolve_target(tenant_id=tenant_id, shop_sku=shop_sku, campaign_id=campaign_id)
        if not offer:
            return {}
        wh_id = self.warehouse_id(tenant_id, warehouse)
        stock_map = offer.get("stock") or {}
        qty = int(stock_map.get(wh_id) or 0)
        return {
            "business_id": offer.get("business_id"),
            "campaign_id": offer.get("campaign_id"),
            "offer_id": offer.get("offer_id"),
            "shop_sku": shop_sku,
            "warehouse": warehouse,
            "warehouse_id": wh_id,
            "available": qty,
            "fulfillment": offer.get("fulfillment"),
            "mode": "FIXTURE",
            "live": False,
        }

    def set_price(self, *, tenant_id: str, shop_sku: str, new_amount: str, campaign_id: str = "") -> tuple[dict, dict, dict]:
        offer, _ = self.resolve_target(tenant_id=tenant_id, shop_sku=shop_sku, campaign_id=campaign_id)
        if not offer:
            return {}, {}, {}
        old = self.read_price(tenant_id=tenant_id, shop_sku=shop_sku, campaign_id=campaign_id)
        offer["seller_price"] = new_amount
        offer["seller_discount_pct"] = "0"
        new = self.read_price(tenant_id=tenant_id, shop_sku=shop_sku, campaign_id=campaign_id)
        return normalize_offer(offer), old, new

    def set_stock(self, *, tenant_id: str, shop_sku: str, warehouse: str, quantity: int, campaign_id: str = "") -> tuple[dict, int]:
        offer, _ = self.resolve_target(tenant_id=tenant_id, shop_sku=shop_sku, campaign_id=campaign_id)
        if not offer:
            return {}, 0
        wh_id = self.warehouse_id(tenant_id, warehouse)
        stock_map = offer.setdefault("stock", {})
        old = int(stock_map.get(wh_id) or 0)
        stock_map[wh_id] = quantity
        return normalize_offer(offer), old

    def create_submission_task(
        self,
        *,
        tenant_id: str,
        payload: dict,
        panda_product_id: str = "",
        initial_status: str = "SUBMITTED",
    ) -> dict:
        scope = self.business_scope(tenant_id)
        task_id = f"ym-sub-{uuid.uuid4().hex[:8]}"
        task = {
            "task_id": task_id,
            "tenant_id": tenant_id,
            "business_id": scope["business_id"],
            "campaign_id": str(payload.get("campaign_id") or scope["default_campaign"]),
            "status": initial_status,
            "offer_id": str(payload.get("offer_id") or payload.get("shop_sku") or ""),
            "payload_summary": {"title": payload.get("title"), "shop_sku": payload.get("shop_sku")},
            "panda_product_id": panda_product_id,
            "mode": "FIXTURE",
            "live": False,
        }
        self._submission_tasks[task_id] = task
        return task

    def get_submission_task(self, task_id: str) -> dict | None:
        return self._submission_tasks.get(task_id)

    def advance_submission_task(self, task_id: str, *, status: str, market_sku: str | None = None) -> dict:
        task = self._submission_tasks.get(task_id)
        if not task:
            return {}
        task["status"] = status
        if market_sku is not None:
            task["market_sku"] = market_sku
        return dict(task)

    def finalize_submission_success(self, *, tenant_id: str, task_id: str) -> dict:
        task = self._submission_tasks.get(task_id)
        if not task:
            return {}
        payload = dict(task.get("payload_summary") or {})
        offer = self.create_offer(
            tenant_id=tenant_id,
            payload={
                "offer_id": task.get("offer_id"),
                "shop_sku": payload.get("shop_sku") or task.get("offer_id"),
                "campaign_id": task.get("campaign_id"),
                "title": payload.get("title") or "Imported",
                "purchase_cost": "450",
            },
            panda_product_id=str(task.get("panda_product_id") or ""),
        )
        self.advance_submission_task(task_id, status="PUBLISHED", market_sku=offer.get("market_sku"))
        return offer

    def create_offer(self, *, tenant_id: str, payload: dict, panda_product_id: str = "") -> dict:
        cat = self.catalog(tenant_id)
        scope = self.business_scope(tenant_id)
        oid = f"ym-offer-{uuid.uuid4().hex[:6]}"
        offer = {
            "business_id": int(payload.get("business_id") or scope["business_id"]),
            "campaign_id": str(payload.get("campaign_id") or scope["default_campaign"]),
            "offer_id": str(payload.get("offer_id") or f"OFFER-{uuid.uuid4().hex[:4]}"),
            "shop_sku": str(payload.get("shop_sku") or payload.get("sku") or ""),
            "market_sku": str(payload.get("market_sku") or f"MKT-{9000000 + len(cat)}"),
            "barcode": str(payload.get("barcode") or ""),
            "title": str(payload.get("title") or payload.get("name") or ""),
            "brand": str(payload.get("brand") or ""),
            "category_id": str(payload.get("category_id") or ""),
            "status": "DRAFT",
            "moderation_state": "PENDING",
            "fulfillment": str(payload.get("fulfillment") or "DBS"),
            "purchase_cost": str(payload.get("purchase_cost") or "0"),
            "seller_price": str(payload.get("seller_price") or payload.get("price") or "0"),
            "old_price": "0",
            "seller_discount_pct": "0",
            "currency": "RUB",
            "platform_promo": None,
            "attributes": dict(payload.get("attributes") or {}),
            "stock": {},
        }
        cat[oid] = offer
        if panda_product_id:
            self._mappings[(tenant_id, panda_product_id)] = oid
        return normalize_offer(offer)

    def list_offers(self, *, tenant_id: str, page: int = 1, page_size: int = 2) -> dict:
        items = [normalize_offer(v) for v in self.catalog(tenant_id).values()]
        start = (page - 1) * page_size
        chunk = items[start : start + page_size]
        next_page = page + 1 if start + page_size < len(items) else None
        return {"items": chunk, "next_page": next_page, "page": page, "bounded": True, "mode": "FIXTURE", "live": False}

    def orders_page(self, *, tenant_id: str, page: int = 1) -> dict:
        scope = self.business_scope(tenant_id)
        items = [
            {
                "order_id": f"ym-order-{page}-{i}",
                "business_id": scope["business_id"],
                "campaign_id": scope["default_campaign"],
                "shop_sku": "YM-SKU-100",
                "offer_id": "OFFER-YM-100",
                "status": "PROCESSING",
                "fulfillment": "DBS",
                "warehouse_id": "wh-dbs-a-main",
                "quantity": 1,
                "total": "1890.00",
                "currency": "RUB",
            }
            for i in range(2)
        ]
        return {"items": items, "page": page, "next_page": page + 1 if page < 5 else None, "bounded": True, "mode": "FIXTURE", "live": False}

    def promotions_page(self, *, tenant_id: str) -> dict:
        return {
            "items": [
                {
                    "promotion_id": "ym-promo-1",
                    "title": "Yandex Market Sale",
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


GLOBAL_YM_CATALOG = YandexMarketCatalogStore()
