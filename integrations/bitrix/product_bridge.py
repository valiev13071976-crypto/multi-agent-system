"""Bitrix ↔ Block 5.5 Product Intelligence bridge (Block 5.6 sections 9/13/14/23/24).

Canonical direction (spec section 3):

    Canonical Panda Product (product_intel) -> this bridge -> Bitrix payload
    Bitrix catalog/offer         -> this bridge -> canonical Panda Product row

Deliberately kept OUTSIDE ``product_intel/`` (the vendor-neutral canonical
core -- see its own no-vendor-strings guard test) and outside
``integrations/bitrix/mapping.py`` (raw Bitrix-schema helpers with no
knowledge of ``product_intel``'s row/dataclass shapes). This module is the
one explicit seam between the two; ``product_intel`` never imports it and
knows nothing about Bitrix.

All external reads/writes are executed through
``IntegrationActivationService.execute_via_gateway`` -- the existing,
already-governed (capability + environment + idempotency + audit) Block 5.4
boundary -- never a raw HTTP client. No second connector/queue/worker
framework is introduced; large jobs reuse the same
``product_intel.planner`` batch-admission gate Block 5.5 already uses for
big imports (which itself routes into the existing Block 3 batch/background
runtime), and idempotency reuses the existing Bitrix fixture/live adapters'
write-cache semantics (see ``integrations.bitrix.fixture_adapter``).
"""

from __future__ import annotations

import hashlib

from integrations.activation.models import ENV_FIXTURE, OP_READ, OP_WRITE
from integrations.bitrix.catalog import GLOBAL_BITRIX_CATALOG, BitrixCatalogStore
from integrations.bitrix.mapping import canonical_to_bitrix_payload
from product_intel.planner import assert_sync_product_allowed
from security.tenant import require_tenant_id

READ_CAPABILITY = "cms.bitrix.catalog.read"
WRITE_CAPABILITY = "cms.bitrix.catalog.write"

SYNC_CREATE = "CREATE"
SYNC_UPDATE = "UPDATE"
SYNC_UNCHANGED = "UNCHANGED"
SYNC_AMBIGUOUS = "AMBIGUOUS"
SYNC_INVALID = "INVALID"
SYNC_SKIP = "SKIP"

SOURCE_BITRIX = "bitrix"


def _bitrix_row(product: dict, *, offer: dict | None = None) -> dict:
    """Map one Bitrix (product[, offer]) pair into a product_intel row shape.

    Each offer becomes its own row/``Product`` (product_intel's "one row ==
    one SKU" convention, spec section 3's Product-vs-Variant note) so
    variants never collapse; ``attributes`` carries the offer's own
    characteristics (color/size/...) so they remain queryable as
    ``variant_attributes`` on the resulting canonical Product.
    """
    props = dict(product.get("properties") or {})
    row: dict = {
        "product_name": product.get("name") or "",
        "brand": props.get("brand", ""),
        "category": product.get("section") or "",
        "description": props.get("description", ""),
        "__bitrix_id": str(product.get("external_product_id") or ""),
        "__bitrix_xml_id": str(product.get("xml_id") or ""),
        "__bitrix_active": bool(product.get("active")),
    }
    if offer:
        row["sku"] = offer.get("article") or ""
        price = dict(offer.get("price") or {})
        stock = dict(offer.get("stock") or {})
        row["__row_ref"] = f"{product.get('external_product_id')}:{offer.get('offer_id')}"
        row["__bitrix_offer_id"] = str(offer.get("offer_id") or "")
        variant = dict(offer.get("variant") or {})
        if variant:
            row["attributes"] = variant
    else:
        row["sku"] = product.get("article") or ""
        prices = product.get("prices") or [{}]
        price = dict(prices[0] if prices else {})
        stock = dict(product.get("stock") or {})
        row["__row_ref"] = row["__bitrix_id"]
        row["__bitrix_offer_id"] = ""
    if price.get("amount") not in (None, ""):
        row["selling_price"] = price.get("amount")
    if price.get("currency"):
        row["currency"] = price.get("currency")
    qty = stock.get("total")
    if qty is not None:
        row["stock"] = qty
    return row


class BitrixProductBridge:
    """Governed Bitrix Connector for Block 5.5 Product Intelligence."""

    def __init__(
        self,
        *,
        integration_activation,
        environment: str = ENV_FIXTURE,
        aspro_enabled: bool = False,
        store: BitrixCatalogStore | None = None,
    ):
        self._activation = integration_activation
        self._environment = environment
        self._aspro_enabled = aspro_enabled
        self._store = store or GLOBAL_BITRIX_CATALOG

    # --- health (spec section 8) --------------------------------------

    def health(self, *, tenant_id: str, connection_id: str) -> dict:
        view = self._activation.health(tenant_id=tenant_id, connection_id=connection_id)
        return {
            "status": view.status,
            "provider": view.provider_id,
            "environment": view.environment,
            "error_category": view.error_category,
        }

    # --- catalog / section read (spec section 10) ----------------------

    def read_sections(self, *, tenant_id: str, connection_id: str | None = None) -> dict:
        out = self._activation.execute_via_gateway(
            tenant_id=tenant_id,
            capability=READ_CAPABILITY,
            environment=self._environment,
            operation_class=OP_READ,
            payload={"operation": "section_read"},
            connection_id=connection_id,
        )
        return out["result"]

    # --- Bitrix -> Panda import (spec section 13) -----------------------

    def import_catalog(
        self,
        *,
        tenant_id: str,
        product_intelligence_service,
        connection_id: str | None = None,
        max_pages: int = 3,
        catalog_id: str | None = None,
        bulk: bool = False,
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        rows: list[dict] = []
        page = 1
        while page <= max_pages:
            out = self._activation.execute_via_gateway(
                tenant_id=tenant,
                capability=READ_CAPABILITY,
                environment=self._environment,
                operation_class=OP_READ,
                payload={},
                connection_id=connection_id,
                page=page,
            )
            items = out["result"].get("items") or []
            for item in items:
                rows.append(_bitrix_row(item))
                for offer in (item.get("offers") or {}).values():
                    rows.append(_bitrix_row(item, offer=offer))
            nxt = out["result"].get("next_page")
            if not nxt:
                break
            page = int(nxt)
        if not rows:
            return {"imported": 0, "created": 0, "updated": 0, "product_ids": (), "total_rows": 0}

        result = product_intelligence_service.import_rows(
            tenant_id=tenant,
            rows=rows,
            source_type=SOURCE_BITRIX,
            source_ref="bitrix-catalog",
            catalog_id=catalog_id,
            bulk=bulk,
        )
        for detail in result.details:
            if detail.get("status") != "OK":
                continue
            row = rows[detail["row"]]
            external_key = row.get("__bitrix_offer_id") or row.get("__bitrix_id")
            if external_key:
                self._store.bind_mapping(
                    tenant_id=tenant, panda_product_id=detail["product_id"], bitrix_id=external_key
                )
        return {
            "imported": result.created + result.updated,
            "created": result.created,
            "updated": result.updated,
            "invalid": result.invalid,
            "ambiguous": result.ambiguous,
            "total_rows": result.total_rows,
            "catalog_id": result.catalog_id,
            "product_ids": result.product_ids,
        }

    # --- Panda -> Bitrix sync diff (spec section 23) --------------------

    def plan_sync(self, *, tenant_id: str, canonical_product: dict) -> dict:
        tenant = require_tenant_id(tenant_id)
        title = str(canonical_product.get("title") or "")
        sku = str(canonical_product.get("sku") or canonical_product.get("article") or "")
        if not title or not sku:
            return {"action": SYNC_INVALID, "reason": "missing_name_or_sku"}

        panda_id = str(canonical_product.get("product_id") or "")
        mapped_bitrix_id = (
            self._store.get_mapping(tenant_id=tenant, panda_product_id=panda_id) if panda_id else None
        )
        target = (
            self._store.lookup(tenant_id=tenant, bitrix_id=mapped_bitrix_id)
            if mapped_bitrix_id
            else self._store.lookup(tenant_id=tenant, article=sku)
        )
        if isinstance(target, list):
            return {
                "action": SYNC_AMBIGUOUS,
                "reason": "multiple_bitrix_targets",
                "candidates": [t.get("external_product_id") for t in target],
            }
        if not target:
            return {
                "action": SYNC_CREATE,
                "mapped_payload": canonical_to_bitrix_payload(
                    product=canonical_product, aspro_enabled=self._aspro_enabled
                ),
            }

        changes: dict = {}
        if title and title != target.get("name"):
            changes["name"] = title
        description = str(canonical_product.get("description") or "")
        if description and description != (target.get("properties") or {}).get("description", ""):
            changes.setdefault("properties", {})["description"] = description
        if not changes:
            return {"action": SYNC_UNCHANGED, "target": target}
        return {"action": SYNC_UPDATE, "target": target, "changes": changes}

    # --- Governed create/update (spec sections 15/16) -------------------

    def sync_product(
        self,
        *,
        tenant_id: str,
        canonical_product: dict,
        idempotency_key: str,
        approved_write: bool = True,
        connection_id: str | None = None,
    ) -> dict:
        plan = self.plan_sync(tenant_id=tenant_id, canonical_product=canonical_product)
        action = plan["action"]
        if action in (SYNC_UNCHANGED, SYNC_SKIP):
            # UNCHANGED must not generate remote mutation (Acceptance O).
            return {"action": action, "mutated": False, "plan": plan}
        if action in (SYNC_AMBIGUOUS, SYNC_INVALID):
            return {"action": action, "mutated": False, "plan": plan}

        if action == SYNC_CREATE:
            payload = {
                "operation": "product_create",
                "panda_product_id": canonical_product.get("product_id"),
                "product": canonical_product,
                "aspro_premier_enabled": self._aspro_enabled,
                "active": True,
            }
        else:
            payload = {
                "operation": "product_update",
                "bitrix_id": plan["target"]["external_product_id"],
                "changes": plan["changes"],
            }

        out = self._activation.execute_via_gateway(
            tenant_id=tenant_id,
            capability=WRITE_CAPABILITY,
            environment=self._environment,
            operation_class=OP_WRITE,
            payload=payload,
            idempotency_key=idempotency_key,
            approved_write=approved_write,
            connection_id=connection_id,
        )
        result = out["result"]
        bitrix_id = (result.get("product") or {}).get("external_product_id") or (
            result.get("mapping") or {}
        ).get("bitrix_id")
        panda_id = str(canonical_product.get("product_id") or "")
        if bitrix_id and panda_id:
            self._store.bind_mapping(tenant_id=tenant_id, panda_product_id=panda_id, bitrix_id=bitrix_id)
        return {
            "action": action,
            "mutated": True,
            "result": result,
            "live": out.get("live"),
            "connection_id": out.get("connection_id"),
        }

    # --- Price / stock sync (spec sections 18/19) -----------------------

    def sync_price(
        self,
        *,
        tenant_id: str,
        canonical_product: dict,
        idempotency_key: str,
        approved_write: bool = True,
        connection_id: str | None = None,
    ) -> dict:
        price = canonical_product.get("price") or {}
        selling = price.get("selling_price")
        if selling in (None, ""):
            return {"action": SYNC_SKIP, "mutated": False, "reason": "no_selling_price"}
        sku = str(canonical_product.get("sku") or canonical_product.get("article") or "")
        if not sku:
            return {"action": SYNC_INVALID, "mutated": False, "reason": "missing_sku"}
        out = self._activation.execute_via_gateway(
            tenant_id=tenant_id,
            capability=WRITE_CAPABILITY,
            environment=self._environment,
            operation_class=OP_WRITE,
            payload={
                "operation": "price_update",
                "article": sku,
                "new_price": str(selling),
                "currency": price.get("currency") or "RUB",
            },
            idempotency_key=idempotency_key,
            approved_write=approved_write,
            connection_id=connection_id,
        )
        return {"action": "PRICE_SYNCED", "mutated": True, "result": out["result"]}

    def sync_stock(
        self,
        *,
        tenant_id: str,
        canonical_product: dict,
        idempotency_key: str,
        approved_write: bool = True,
        connection_id: str | None = None,
    ) -> dict:
        stock = canonical_product.get("stock") or {}
        qty = stock.get("quantity")
        if qty in (None, ""):
            # Unknown stock must never silently become zero (spec section 19).
            return {"action": SYNC_SKIP, "mutated": False, "reason": "unknown_stock_not_written"}
        sku = str(canonical_product.get("sku") or canonical_product.get("article") or "")
        if not sku:
            return {"action": SYNC_INVALID, "mutated": False, "reason": "missing_sku"}
        out = self._activation.execute_via_gateway(
            tenant_id=tenant_id,
            capability=WRITE_CAPABILITY,
            environment=self._environment,
            operation_class=OP_WRITE,
            payload={"operation": "stock_update", "article": sku, "quantity": int(float(qty))},
            idempotency_key=idempotency_key,
            approved_write=approved_write,
            connection_id=connection_id,
        )
        return {"action": "STOCK_SYNCED", "mutated": True, "result": out["result"]}

    # --- Media association (spec section 20) ----------------------------

    def associate_media(
        self,
        *,
        tenant_id: str,
        canonical_product: dict,
        idempotency_key: str,
        approved_write: bool = True,
        connection_id: str | None = None,
    ) -> dict:
        media_refs = [str(m) for m in (canonical_product.get("media_refs") or ()) if m]
        if not media_refs:
            return {"action": SYNC_SKIP, "mutated": False, "reason": "no_media"}
        sku = str(canonical_product.get("sku") or canonical_product.get("article") or "")
        panda_id = str(canonical_product.get("product_id") or "")
        out = self._activation.execute_via_gateway(
            tenant_id=tenant_id,
            capability=WRITE_CAPABILITY,
            environment=self._environment,
            operation_class=OP_WRITE,
            payload={
                "operation": "media_attach",
                "article": sku,
                "panda_product_id": panda_id,
                "media_refs": media_refs,
            },
            idempotency_key=idempotency_key,
            approved_write=approved_write,
            connection_id=connection_id,
        )
        result = out["result"]
        return {
            "action": "MEDIA_ATTACHED",
            "mutated": bool(result.get("added")),
            "added": result.get("added"),
            "duplicate_skipped": result.get("duplicate_skipped"),
            "result": result,
        }

    # --- Content / SEO sync (spec section 21) ----------------------------

    def sync_seo(
        self,
        *,
        tenant_id: str,
        canonical_product: dict,
        idempotency_key: str,
        approved_write: bool = True,
        connection_id: str | None = None,
    ) -> dict:
        seo_title = str(canonical_product.get("seo_title") or "")
        seo_description = str(canonical_product.get("seo_description") or "")
        if not seo_title and not seo_description:
            return {"action": SYNC_SKIP, "mutated": False, "reason": "no_seo_content"}
        sku = str(canonical_product.get("sku") or canonical_product.get("article") or "")
        out = self._activation.execute_via_gateway(
            tenant_id=tenant_id,
            capability=WRITE_CAPABILITY,
            environment=self._environment,
            operation_class=OP_WRITE,
            payload={
                "operation": "seo_update",
                "article": sku,
                "seo_title": seo_title,
                "seo_description": seo_description,
            },
            idempotency_key=idempotency_key,
            approved_write=approved_write,
            connection_id=connection_id,
        )
        return {"action": "SEO_SYNCED", "mutated": True, "result": out["result"]}

    # --- Bulk catalog sync (spec section 24, reuses product_intel.planner) --

    def bulk_sync(
        self,
        *,
        tenant_id: str,
        canonical_products: list[dict],
        correlation_prefix: str = "bitrix-bulk",
        approved_write: bool = True,
        connection_id: str | None = None,
        bulk: bool = False,
    ) -> dict:
        # Small jobs execute inline; large jobs must be explicitly routed as
        # ``bulk=True`` through the existing batch/background runtime --
        # mirrors ``product_intel.service.import_rows``'s own gate exactly
        # (spec section 24: reuse, never a second job queue).
        assert_sync_product_allowed(item_count=len(canonical_products), bulk=bulk)
        results = []
        succeeded = failed = 0
        for cp in canonical_products:
            pid = str(cp.get("product_id") or "")
            key = f"{correlation_prefix}:{pid}:{self._content_hash(cp)}"
            try:
                r = self.sync_product(
                    tenant_id=tenant_id,
                    canonical_product=cp,
                    idempotency_key=key,
                    approved_write=approved_write,
                    connection_id=connection_id,
                )
                results.append({"product_id": pid, **r})
                if r.get("action") in (SYNC_AMBIGUOUS, SYNC_INVALID):
                    failed += 1
                else:
                    succeeded += 1
            except Exception as exc:  # noqa: BLE001 -- normalize, never leak raw adapter exceptions
                failed += 1
                results.append(
                    {
                        "product_id": pid,
                        "action": "ERROR",
                        "mutated": False,
                        "error": getattr(exc, "code", type(exc).__name__),
                    }
                )
        return {
            "total": len(canonical_products),
            "succeeded": succeeded,
            "failed": failed,
            "items": results,
            "bounded": True,
        }

    @staticmethod
    def _content_hash(canonical_product: dict) -> str:
        price = canonical_product.get("price") or {}
        stock = canonical_product.get("stock") or {}
        basis = "|".join(
            str(canonical_product.get(k) or "")
            for k in ("title", "sku", "description", "seo_title", "seo_description")
        )
        basis += f"|{price.get('selling_price') or ''}|{stock.get('quantity') or ''}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
