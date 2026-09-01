"""Tenant-scoped 1C nomenclature fixture + normalization."""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal
from typing import Any


DEFAULT_WAREHOUSES = {
    "wh-main": {"warehouse_id": "wh-main", "name": "Main Warehouse", "panda_ref": "main"},
    "wh-east": {"warehouse_id": "wh-east", "name": "East Warehouse", "panda_ref": "east"},
}


def _seed_nomenclature() -> dict[str, dict[str, dict]]:
    base = {
        "guid-prod-1001": {
            "guid": "guid-prod-1001",
            "external_id": "guid-prod-1001",
            "xml_id": "1C-XML-1001",
            "article": "1C-SKU-100",
            "name": "Industrial Pump A",
            "unit": "pcs",
            "active": True,
            "organization_id": "org-default",
            "prices": [
                {"price_type": "RETAIL", "amount": "45000.00", "currency": "RUB", "effective_date": "2026-01-01"},
                {"price_type": "PURCHASE", "amount": "32000.00", "currency": "RUB", "effective_date": "2026-01-01"},
            ],
            "variants": {
                "var-red": {
                    "characteristic_id": "var-red",
                    "article": "1C-SKU-100-RED",
                    "name": "Industrial Pump A (Red)",
                    "prices": [{"price_type": "RETAIL", "amount": "46000.00", "currency": "RUB"}],
                    "stock": {
                        "wh-main": {"available": 5, "physical": 6, "reserved": 1},
                        "wh-east": {"available": 2, "physical": 2, "reserved": 0},
                    },
                },
            },
            "stock": {
                "wh-main": {"available": 10, "physical": 12, "reserved": 2},
                "wh-east": {"available": 4, "physical": 4, "reserved": 0},
            },
        },
        "guid-prod-1002": {
            "guid": "guid-prod-1002",
            "external_id": "guid-prod-1002",
            "xml_id": "1C-XML-1002",
            "article": "1C-SKU-200",
            "name": "Valve Set B",
            "unit": "pcs",
            "active": True,
            "organization_id": "org-default",
            "prices": [{"price_type": "RETAIL", "amount": "8900.00", "currency": "RUB"}],
            "variants": {},
            "stock": {"wh-main": {"available": 25, "physical": 25, "reserved": 0}},
        },
        "guid-prod-amb": {
            "guid": "guid-prod-amb",
            "external_id": "guid-prod-amb",
            "xml_id": "1C-XML-AMB",
            "article": "1C-SKU-AMB",
            "name": "Generic Part",
            "unit": "pcs",
            "active": True,
            "organization_id": "org-default",
            "prices": [{"price_type": "RETAIL", "amount": "1000.00", "currency": "RUB"}],
            "variants": {},
            "stock": {"wh-main": {"available": 1, "physical": 1, "reserved": 0}},
            "ambiguous_duplicate_name": True,
        },
    }
    return {tid: copy.deepcopy(base) for tid in ("tenant-a", "tenant-b", "default")}


def normalize_nomenclature(raw: dict) -> dict:
    return {
        "guid": raw.get("guid") or raw.get("external_id") or "",
        "external_id": raw.get("external_id") or raw.get("guid") or "",
        "xml_id": raw.get("xml_id") or "",
        "article": raw.get("article") or raw.get("sku") or "",
        "name": raw.get("name") or "",
        "unit": raw.get("unit") or "pcs",
        "active": bool(raw.get("active", True)),
        "organization_id": raw.get("organization_id") or "",
        "prices": list(raw.get("prices") or []),
        "variants": {
            vid: {
                "characteristic_id": v.get("characteristic_id"),
                "article": v.get("article"),
                "name": v.get("name"),
                "prices": list(v.get("prices") or []),
                "stock": dict(v.get("stock") or {}),
            }
            for vid, v in (raw.get("variants") or {}).items()
        },
        "stock": dict(raw.get("stock") or {}),
        "mode": "FIXTURE",
        "live": False,
    }


class OneCCatalogStore:
    """Tenant-scoped 1C catalog, documents, mappings."""

    def __init__(self):
        self._catalogs: dict[str, dict[str, dict]] = _seed_nomenclature()
        self._documents: dict[str, dict[str, dict]] = {}
        self._mappings: dict[tuple[str, str], str] = {}
        self._write_counts: dict[str, int] = {}
        self._warehouse_map: dict[str, dict[str, str]] = {
            "tenant-a": {"main": "wh-main", "east": "wh-east"},
            "tenant-b": {"main": "wh-main"},
            "default": {"main": "wh-main", "east": "wh-east"},
        }

    def catalog(self, tenant_id: str) -> dict[str, dict]:
        tid = tenant_id or "default"
        if tid not in self._catalogs:
            self._catalogs[tid] = copy.deepcopy(self._catalogs["default"])
        return self._catalogs[tid]

    def documents(self, tenant_id: str) -> dict[str, dict]:
        tid = tenant_id or "default"
        if tid not in self._documents:
            self._documents[tid] = {}
        return self._documents[tid]

    def warehouse_id(self, tenant_id: str, panda_warehouse: str) -> str:
        mapping = self._warehouse_map.get(tenant_id or "default", {})
        wh = mapping.get(panda_warehouse)
        if not wh:
            raise KeyError("warehouse_not_mapped")
        return wh

    def list_products(self, *, tenant_id: str, page: int = 1, page_size: int = 2) -> dict:
        cat = self.catalog(tenant_id)
        items = [normalize_nomenclature(v) for v in cat.values()]
        start = (page - 1) * page_size
        chunk = items[start : start + page_size]
        next_page = page + 1 if start + page_size < len(items) else None
        return {"items": chunk, "next_page": next_page, "page": page, "bounded": True, "mode": "FIXTURE", "live": False}

    def lookup(
        self,
        *,
        tenant_id: str,
        guid: str = "",
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
                return normalize_nomenclature(cat[mapped])

        matches = []
        for prod in cat.values():
            if guid and prod.get("guid") == guid:
                matches.append(prod)
            elif xml_id and prod.get("xml_id") == xml_id:
                matches.append(prod)
            elif article and (
                prod.get("article") == article
                or any(v.get("article") == article for v in (prod.get("variants") or {}).values())
            ):
                matches.append(prod)
            elif name and prod.get("name", "").casefold() == name.casefold():
                matches.append(prod)

        if not matches:
            return {}
        if len(matches) > 1 and not allow_name_only:
            return matches
        if len(matches) > 1:
            return matches
        return normalize_nomenclature(matches[0])

    def resolve_variant(self, *, tenant_id: str, article: str) -> tuple[dict, dict] | None:
        cat = self.catalog(tenant_id)
        for prod in cat.values():
            for vid, var in (prod.get("variants") or {}).items():
                if var.get("article") == article:
                    return prod, var
            if prod.get("article") == article:
                return prod, {}
        return None

    def read_price(self, *, tenant_id: str, article: str, price_type: str = "RETAIL") -> dict:
        resolved = self.resolve_variant(tenant_id=tenant_id, article=article)
        if not resolved:
            return {}
        prod, var = resolved
        prices = (var.get("prices") if var else None) or prod.get("prices") or []
        match = next((p for p in prices if p.get("price_type") == price_type), prices[0] if prices else {})
        return {
            "article": article,
            "guid": prod.get("guid"),
            "characteristic_id": var.get("characteristic_id") if var else "",
            "price_type": match.get("price_type", price_type),
            "amount": str(match.get("amount", "0")),
            "currency": match.get("currency", "RUB"),
            "effective_date": match.get("effective_date", ""),
            "mode": "FIXTURE",
            "live": False,
        }

    def read_stock(self, *, tenant_id: str, article: str, warehouse: str = "main") -> dict:
        resolved = self.resolve_variant(tenant_id=tenant_id, article=article)
        if not resolved:
            return {}
        prod, var = resolved
        wh_id = self.warehouse_id(tenant_id, warehouse)
        stock_map = (var.get("stock") if var else None) or prod.get("stock") or {}
        wh_stock = stock_map.get(wh_id) or {}
        return {
            "article": article,
            "guid": prod.get("guid"),
            "warehouse_id": wh_id,
            "warehouse": warehouse,
            "available": int(wh_stock.get("available") or 0),
            "physical": int(wh_stock.get("physical") or 0),
            "reserved": int(wh_stock.get("reserved") or 0),
            "mode": "FIXTURE",
            "live": False,
        }

    def set_price(
        self,
        *,
        tenant_id: str,
        article: str,
        new_amount: str,
        price_type: str = "RETAIL",
        currency: str = "RUB",
    ) -> tuple[dict, dict, dict]:
        resolved = self.resolve_variant(tenant_id=tenant_id, article=article)
        if not resolved:
            return {}, {}, {}
        prod, var = resolved
        if var:
            prices = var.setdefault("prices", [])
            old = next((dict(p) for p in prices if p.get("price_type") == price_type), {})
            new_p = {"price_type": price_type, "amount": new_amount, "currency": currency}
            replaced = False
            for i, p in enumerate(prices):
                if p.get("price_type") == price_type:
                    prices[i] = new_p
                    replaced = True
                    break
            if not replaced:
                prices.append(new_p)
        else:
            prices = prod.setdefault("prices", [])
            old = next((dict(p) for p in prices if p.get("price_type") == price_type), {})
            new_p = {"price_type": price_type, "amount": new_amount, "currency": currency, "effective_date": "2026-09-01"}
            replaced = False
            for i, p in enumerate(prices):
                if p.get("price_type") == price_type:
                    prices[i] = new_p
                    replaced = True
                    break
            if not replaced:
                prices.append(new_p)
        return normalize_nomenclature(prod), old, new_p

    def create_document(
        self,
        *,
        tenant_id: str,
        document_type: str,
        payload: dict,
        idempotency_key: str = "",
    ) -> dict:
        docs = self.documents(tenant_id)
        doc_id = f"1c-doc-{uuid.uuid4().hex[:8]}"
        doc = {
            "document_id": doc_id,
            "document_type": document_type,
            "number": f"DOC-{len(docs)+1:05d}",
            "status": "DRAFT",
            "posted": False,
            "organization_id": payload.get("organization_id") or "org-default",
            "counterparty": payload.get("counterparty") or "",
            "currency": payload.get("currency") or "RUB",
            "total": str(payload.get("total") or "0"),
            "items": list(payload.get("items") or []),
        }
        docs[doc_id] = doc
        return doc

    def list_orders(self, *, tenant_id: str, page: int = 1) -> dict:
        docs = self.documents(tenant_id)
        items = [
            {
                "document_id": d["document_id"],
                "number": d["number"],
                "status": d["status"],
                "total": d["total"],
                "currency": d["currency"],
                "items_summary": d.get("items") or [],
            }
            for d in docs.values()
        ] or [
            {
                "document_id": f"onec-order-{page}-{i}",
                "number": f"ORD-{page}{i}",
                "status": "NEW",
                "total": str(Decimal("5000") * (i + 1)),
                "currency": "RUB",
                "items_summary": [{"article": "1C-SKU-100", "qty": 1}],
            }
            for i in range(2)
        ]
        return {"items": items, "page": page, "next_page": page + 1 if page < 5 else None, "bounded": True, "mode": "FIXTURE", "live": False}

    def record_write(self, key: str) -> int:
        self._write_counts[key] = self._write_counts.get(key, 0) + 1
        return self._write_counts[key]

    def write_count(self, key: str) -> int:
        return self._write_counts.get(key, 0)

    def bind_mapping(self, *, tenant_id: str, panda_product_id: str, guid: str) -> None:
        self._mappings[(tenant_id, panda_product_id)] = guid

    def get_mapping(self, *, tenant_id: str, panda_product_id: str) -> str | None:
        return self._mappings.get((tenant_id, panda_product_id))


GLOBAL_ONEC_CATALOG = OneCCatalogStore()
