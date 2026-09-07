"""Product Intelligence Service — Block 5.5 governed facade.

Canonical high-level chain (spec section 1):

    Excel(5.1) / Acquisition(5.2) / Artifact / Payload
        -> field mapping + normalization (this module, reusing 5.1/5.2 primitives)
        -> canonical Product model
        -> matching / dedupe / validation
        -> Content(5.3) enrichment, Media/Artifact association
        -> canonical vendor-neutral export
        -> Block 5.4 governed Tool & Integration boundary
        -> (future) CRM / marketplace / ERP adapters.

Design note on Product vs. ProductVariant (spec section 3): each ingested
source row becomes exactly one flat ``Product`` record (one row == one SKU).
This is the simplest, safest mapping -- two distinct SKUs (e.g. a 128GB and a
256GB variant) can never accidentally collapse into a single record, because
they are never represented as a single record to begin with (spec section 8:
"must NOT become one SKU merely because the base title is similar"). Rows
that share the exact same normalized brand+title are additionally tagged
with a common ``variant_group_key`` so a caller can recover sibling variants
without needing to merge them. ``ProductVariant`` remains available for
callers that want to explicitly nest sibling SKUs under one parent record.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from data_intel.compare import reconcile_stock as _reconcile_stock_rows
from data_intel.mapping import role_map as _role_map

from product_intel.errors import (
    PRODUCT_EXECUTION_FAILED,
    PRODUCT_NOT_FOUND,
    PRODUCT_SOURCE_NOT_FOUND,
    ProductIntelError,
)
from product_intel.matching import match_against_catalog, match_pair
from product_intel.normalize import (
    normalize_attribute_key,
    normalize_attribute_value,
    normalize_brand,
    normalize_category,
    normalize_currency,
    normalize_ean,
    normalize_mpn,
    normalize_price,
    normalize_sku,
    normalize_stock_quantity,
    normalize_title,
)
from product_intel.planner import assert_sync_product_allowed
from product_intel.platform_models import (
    AVAIL_IN_STOCK,
    AVAIL_OUT_OF_STOCK,
    MATCH_STATE_EXACT,
    MATCH_STATE_HIGH_CONFIDENCE,
    PROV_DERIVED,
    PROV_GENERATED,
    PROV_SOURCE,
    SCHEMA_VERSION,
    SOURCE_ACQUISITION,
    SOURCE_EXCEL,
    VALIDATION_INVALID,
    PriceInfo,
    Product,
    ProductDuplicateGroup,
    ProductImportResult,
    SourceReference,
    StockInfo,
)
from product_intel.access import ProductAccessPolicy
from product_intel.validation import validate_catalog as _validate_catalog_rows
from product_intel.validation import validate_product
from security.tenant import require_tenant_id

_VARIANT_KEY_ALIASES = {
    "color": "color",
    "цвет": "color",
    "size": "size",
    "размер": "size",
    "memory": "memory",
    "память": "memory",
    "volume": "volume",
    "объем": "volume",
    "объём": "volume",
    "configuration": "configuration",
    "конфигурация": "configuration",
    "ram": "ram",
}


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _row_get(row: dict, *keys: str):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    lower_map = {str(k).lower(): v for k, v in row.items()}
    for k in keys:
        v = lower_map.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _extract_attributes(row: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in row.items():
        if str(k).startswith("__") or v in (None, ""):
            continue
        canonical = _VARIANT_KEY_ALIASES.get(normalize_attribute_key(k))
        if canonical:
            out[canonical] = normalize_attribute_value(str(v))
    nested = row.get("attributes")
    if isinstance(nested, dict):
        for k, v in nested.items():
            key_norm = normalize_attribute_key(k)
            if key_norm and v not in (None, ""):
                out.setdefault(key_norm, normalize_attribute_value(str(v)))
    return out


def build_product_from_row(
    row: dict,
    *,
    tenant_id: str,
    owner_id: str = "",
    source_type: str = "",
    source_ref: str = "",
    row_ref: str = "",
) -> tuple[Product | None, str]:
    """Map one already role-mapped source row into the canonical Product model
    (spec sections 4/5). Returns ``(None, "AMBIGUOUS_MAPPING")`` when the row
    carries no usable identity at all, instead of guessing.
    """

    title_raw = _row_get(row, "product_name", "name", "title", "товар", "наименование")
    sku_raw = _row_get(row, "sku", "article", "vendor_code", "код", "артикул")
    brand_raw = _row_get(row, "brand", "бренд", "производитель")
    category_raw = _row_get(row, "category", "категория")
    ean_raw = _row_get(row, "ean", "gtin", "barcode", "штрихкод")
    mpn_raw = _row_get(row, "mpn")
    description_raw = _row_get(row, "description", "описание")

    title = str(title_raw or "").strip()
    sku = normalize_sku(sku_raw) or ""
    gtin = normalize_ean(ean_raw) or ""
    if not title and not sku and not gtin:
        return None, "AMBIGUOUS_MAPPING"

    brand = str(brand_raw or "").strip()
    category = normalize_category(category_raw)
    mpn = normalize_mpn(mpn_raw) or ""
    description = str(description_raw or "").strip()

    purchase_raw = _row_get(row, "purchase_price", "cost", "закупка", "закупочная_цена")
    selling_raw = _row_get(row, "selling_price", "розница", "продажная_цена")
    generic_price_raw = _row_get(row, "price", "цена")
    currency_raw = _row_get(row, "currency", "валюта")
    stock_raw = _row_get(row, "stock", "quantity", "available_stock", "количество", "остаток")

    purchase = normalize_price(purchase_raw)
    selling = normalize_price(selling_raw)
    if selling is None:
        selling = normalize_price(generic_price_raw)
    currency = normalize_currency(currency_raw)
    qty = normalize_stock_quantity(stock_raw)
    availability = AVAIL_IN_STOCK if (qty is not None and qty > 0) else (
        AVAIL_OUT_OF_STOCK if qty is not None else "UNKNOWN"
    )

    attributes = _extract_attributes(row)
    normalized_title = normalize_title(title)
    normalized_brand = normalize_brand(brand)
    variant_group_key = f"{normalized_brand}|{normalized_title}" if (normalized_brand or normalized_title) else ""

    field_provenance: dict[str, str] = {}
    for field_name, raw_val in (
        ("title", title_raw),
        ("sku", sku_raw),
        ("brand", brand_raw),
        ("category", category_raw),
        ("gtin", ean_raw),
        ("mpn", mpn_raw),
        ("description", description_raw),
    ):
        if raw_val not in (None, ""):
            field_provenance[field_name] = PROV_SOURCE
    if purchase is not None or selling is not None:
        field_provenance["price"] = PROV_SOURCE
    if qty is not None:
        field_provenance["stock"] = PROV_SOURCE

    source = SourceReference(
        source_type=source_type,
        source_ref=source_ref,
        row_ref=row_ref or str(row.get("__row_ref") or ""),
        raw_snapshot={k: v for k, v in row.items() if not str(k).startswith("__raw")},
    )

    product = Product(
        product_id=str(uuid.uuid4()),
        tenant_id=require_tenant_id(tenant_id),
        owner_id=owner_id,
        title=title,
        normalized_title=normalized_title,
        brand=brand,
        normalized_brand=normalized_brand,
        category=category,
        sku=sku,
        article=sku,
        gtin=gtin,
        mpn=mpn,
        description=description,
        attributes=attributes,
        variant_attributes=attributes,
        variant_group_key=variant_group_key,
        price=PriceInfo(
            currency=currency,
            purchase_price=purchase,
            selling_price=selling,
            source=source_type,
            observed_at=_utc(),
        ),
        stock=StockInfo(quantity=qty, availability=availability, source=source_type, observed_at=_utc()),
        source=source,
        field_provenance=field_provenance,
    )
    return product, ""


class ProductIntelligenceService:
    def __init__(
        self,
        store,
        *,
        access: ProductAccessPolicy | None = None,
        data_intelligence_service=None,
        content_intelligence_service=None,
        artifact_service=None,
        observability=None,
    ):
        self.store = store
        self.access = access or ProductAccessPolicy()
        self.data_intelligence_service = data_intelligence_service
        self.content_intelligence_service = content_intelligence_service
        self.artifact_service = artifact_service
        self.obs = observability

    def _emit(self, event: str, **meta) -> None:
        if self.obs is None:
            return
        try:
            self.obs.emit(event, **meta)
        except Exception:
            pass

    # --- Ingestion (spec section 4) -----------------------------------------

    def import_rows(
        self,
        *,
        tenant_id: str,
        rows: list[dict],
        source_type: str,
        source_ref: str = "",
        catalog_id: str | None = None,
        owner_id: str = "",
        bulk: bool = False,
    ) -> ProductImportResult:
        tenant = require_tenant_id(tenant_id)
        assert_sync_product_allowed(item_count=len(rows), bulk=bulk)
        cid = catalog_id or f"catalog-{tenant}"
        created = updated = invalid = ambiguous = 0
        product_ids: list[str] = []
        details: list[dict] = []
        for idx, row in enumerate(rows):
            product, reason = build_product_from_row(
                row,
                tenant_id=tenant,
                owner_id=owner_id,
                source_type=source_type,
                source_ref=source_ref,
                row_ref=str(row.get("__row_ref") or idx),
            )
            if product is None:
                ambiguous += 1
                details.append({"row": idx, "status": "AMBIGUOUS_MAPPING", "reason": reason})
                continue

            existing = None
            if product.sku:
                existing = self.store.get_by_sku(product.sku, tenant_id=tenant)
            if existing is None and product.gtin:
                existing = self.store.get_by_barcode(product.gtin, tenant_id=tenant)
            if existing is not None:
                product = replace(
                    product,
                    product_id=existing.product_id,
                    created_at=existing.created_at,
                    version=existing.version + 1,
                    media_refs=existing.media_refs,
                    content_refs=existing.content_refs,
                )
                updated += 1
            else:
                created += 1

            validation = validate_product(product)
            product = replace(product, validation_state=validation.state)
            if validation.state == VALIDATION_INVALID:
                invalid += 1

            self.store.save_product(product, catalog_id=cid)
            product_ids.append(product.product_id)
            details.append(
                {
                    "row": idx,
                    "status": "OK",
                    "product_id": product.product_id,
                    "validation_state": validation.state,
                    "issues": [i.code for i in validation.issues],
                }
            )

        result = ProductImportResult(
            import_id=str(uuid.uuid4()),
            tenant_id=tenant,
            catalog_id=cid,
            total_rows=len(rows),
            created=created,
            updated=updated,
            invalid=invalid,
            ambiguous=ambiguous,
            product_ids=tuple(product_ids),
            details=tuple(details),
        )
        self._emit(
            "product.import",
            tenant=tenant,
            catalog_id=cid,
            total=len(rows),
            created=created,
            updated=updated,
            invalid=invalid,
            ambiguous=ambiguous,
            source=source_type,
        )
        return result

    def import_from_excel_dataset(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
        catalog_id: str | None = None,
        owner_id: str = "",
        bulk: bool = False,
    ) -> ProductImportResult:
        if self.data_intelligence_service is None:
            raise ProductIntelError(PRODUCT_SOURCE_NOT_FOUND, "data_intelligence_unavailable")
        tenant = require_tenant_id(tenant_id)
        rows = self.data_intelligence_service.store.get_rows(dataset_id, tenant_id=tenant)
        if not rows:
            raise ProductIntelError(PRODUCT_SOURCE_NOT_FOUND, "dataset_empty_or_not_found")
        return self.import_rows(
            tenant_id=tenant,
            rows=rows,
            source_type=SOURCE_EXCEL,
            source_ref=dataset_id,
            catalog_id=catalog_id,
            owner_id=owner_id,
            bulk=bulk,
        )

    def import_from_acquisition(
        self,
        *,
        tenant_id: str,
        records: list,
        catalog_id: str | None = None,
        owner_id: str = "",
        bulk: bool = False,
    ) -> ProductImportResult:
        if self.data_intelligence_service is None:
            raise ProductIntelError(PRODUCT_SOURCE_NOT_FOUND, "data_intelligence_unavailable")
        tenant = require_tenant_id(tenant_id)
        bridge = self.data_intelligence_service.from_acquisition_records(records, tenant_id=tenant)
        dataset_id = bridge["dataset_id"]
        rows = self.data_intelligence_service.store.get_rows(dataset_id, tenant_id=tenant)
        desc = self.data_intelligence_service.store.get_dataset(dataset_id, tenant_id=tenant)
        if desc is not None and desc.tables:
            roles = _role_map(desc.tables[0].columns)
            for r in rows:
                for col, role in roles.items():
                    if role and role != "unknown" and col in r and role not in r:
                        r[role] = r[col]
        return self.import_rows(
            tenant_id=tenant,
            rows=rows,
            source_type=SOURCE_ACQUISITION,
            source_ref=dataset_id,
            catalog_id=catalog_id,
            owner_id=owner_id,
            bulk=bulk,
        )

    # --- Matching / dedupe (spec section 7/8) -------------------------------

    def match_candidate(self, *, tenant_id: str, candidate: dict, catalog_id: str | None = None):
        tenant = require_tenant_id(tenant_id)
        existing = self.store.list_products(tenant_id=tenant, catalog_id=catalog_id)
        return match_against_catalog(candidate, existing)

    def find_duplicate_groups(self, *, tenant_id: str, catalog_id: str | None = None) -> list[ProductDuplicateGroup]:
        tenant = require_tenant_id(tenant_id)
        products = self.store.list_products(tenant_id=tenant, catalog_id=catalog_id)
        groups: list[ProductDuplicateGroup] = []
        seen: set[str] = set()
        for i, p in enumerate(products):
            if p.product_id in seen:
                continue
            cluster = [p.product_id]
            for q in products[i + 1 :]:
                if q.product_id in seen:
                    continue
                outcome = match_pair(p, q)
                if outcome.state in {MATCH_STATE_EXACT, MATCH_STATE_HIGH_CONFIDENCE}:
                    cluster.append(q.product_id)
                    seen.add(q.product_id)
            if len(cluster) > 1:
                seen.update(cluster)
                groups.append(
                    ProductDuplicateGroup(
                        tenant_id=tenant, product_ids=tuple(cluster), state=MATCH_STATE_EXACT, method="pairwise_identifier"
                    )
                )
        return groups

    # --- Validation (spec section 15) ---------------------------------------

    def validate_catalog(self, *, tenant_id: str, catalog_id: str | None = None) -> dict:
        tenant = require_tenant_id(tenant_id)
        products = self.store.list_products(tenant_id=tenant, catalog_id=catalog_id)
        results = _validate_catalog_rows(products)
        for pid, res in results.items():
            product = self.store.get_product(pid, tenant_id=tenant)
            if product is not None and product.validation_state != res.state:
                self.store.save_product(replace(product, validation_state=res.state), catalog_id=catalog_id)
        return results

    # --- Stock reconciliation (spec section 12, reuses Block 5.1) ----------

    def reconcile_stock_from_dataset(
        self, *, tenant_id: str, dataset_id: str, catalog_id: str | None = None
    ) -> dict:
        if self.data_intelligence_service is None:
            raise ProductIntelError(PRODUCT_SOURCE_NOT_FOUND, "data_intelligence_unavailable")
        tenant = require_tenant_id(tenant_id)
        incoming_rows = self.data_intelligence_service.store.get_rows(dataset_id, tenant_id=tenant)
        products = self.store.list_products(tenant_id=tenant, catalog_id=catalog_id)

        # ``sku`` and ``article`` are, in practice, aliases for the same
        # "vendor code" concept -- different sources (and different Block
        # 5.1 column headers, e.g. "SKU" vs. "Артикул") land the same
        # merchant-assigned code under either role. ``data_intel.compare``'s
        # ``_product_key`` picks exactly one identifier per row by a fixed
        # priority (ean > mpn > sku > article), so a catalog product stored
        # under ``sku`` would never key-match an incoming row whose value
        # only populated ``article`` (or vice versa), even though both
        # denote the identical code. Cross-filling the two roles per-row
        # (never across different rows/products) keeps the identifier
        # namespaces from colliding while letting the reused reconciliation
        # primitive line up the two datasets correctly.
        def _cross_fill(row: dict) -> dict:
            row = dict(row)
            sku_val = row.get("sku") or row.get("article")
            article_val = row.get("article") or row.get("sku")
            if sku_val:
                row["sku"] = sku_val
            if article_val:
                row["article"] = article_val
            return row

        incoming_rows = [_cross_fill(r) for r in incoming_rows]
        catalog_rows = [
            _cross_fill(
                {
                    "sku": p.sku,
                    "article": p.article,
                    "ean": p.gtin,
                    "mpn": p.mpn,
                    "product_name": p.title,
                    "stock": str(p.stock.quantity) if p.stock.quantity is not None else "",
                }
            )
            for p in products
        ]
        report = _reconcile_stock_rows(incoming_rows, catalog_rows)
        lookup_by_prefix = {
            "ean": {p.gtin.upper(): p for p in products if p.gtin},
            "mpn": {p.mpn.upper(): p for p in products if p.mpn},
            "sku": {p.sku.upper(): p for p in products if p.sku},
            "article": {p.article.upper(): p for p in products if p.article},
        }
        updated_ids: list[str] = []
        for item in list(report.get("discrepancy") or []) + list(report.get("matched") or []):
            key = str(item.get("key") or "")
            prefix, _, value = key.partition(":")
            lookup = lookup_by_prefix.get(prefix)
            if lookup is None:
                continue
            product = lookup.get(value.upper())
            if product is None:
                continue
            qty = normalize_stock_quantity(item.get("supplier_stock"))
            if qty is None:
                continue
            new_stock = StockInfo(quantity=qty, availability=AVAIL_IN_STOCK if qty > 0 else AVAIL_OUT_OF_STOCK, source=dataset_id, observed_at=_utc())
            new_prov = dict(product.field_provenance)
            new_prov["stock"] = PROV_SOURCE
            new_product = replace(product, stock=new_stock, field_provenance=new_prov)
            self.store.save_product(new_product, catalog_id=catalog_id)
            updated_ids.append(product.product_id)
        self._emit("product.reconcile", tenant=tenant, dataset_id=dataset_id, updated=len(updated_ids))
        return {
            "updated_count": len(updated_ids),
            "updated_product_ids": updated_ids,
            "summary": {
                "matched": len(report.get("matched") or []),
                "discrepancy": len(report.get("discrepancy") or []),
                "missing": len(report.get("missing") or []),
            },
        }

    # --- Content enrichment (spec section 13, reuses Block 5.3) -------------

    def enrich_product_content(
        self,
        *,
        tenant_id: str,
        product_id: str,
        fields: tuple[str, ...] = ("description", "short_description", "seo_title", "seo_description"),
        channel: str = "marketplace",
    ) -> dict:
        if self.content_intelligence_service is None:
            raise ProductIntelError(PRODUCT_EXECUTION_FAILED, "content_intelligence_unavailable")
        tenant = require_tenant_id(tenant_id)
        product = self.store.get_product(product_id, tenant_id=tenant)
        if product is None:
            raise ProductIntelError(PRODUCT_NOT_FOUND, "product_not_found")

        facts: dict[str, str] = {}
        if product.title:
            facts["title"] = product.title
        if product.brand:
            facts["brand"] = product.brand
        if product.category:
            facts["category"] = product.category
        if product.sku:
            facts["sku"] = product.sku
        if product.price.selling_price is not None:
            facts["price"] = str(product.price.selling_price)
        if product.stock.quantity is not None:
            facts["stock"] = str(product.stock.quantity)
        facts.update(dict(product.attributes))

        asset = self.content_intelligence_service.generate_copy(
            tenant_id=tenant,
            project_id=f"product-{product_id}",
            content_type="product_description",
            channel=channel,
            objective=f"Describe {product.title or product.sku or product.gtin}",
            product_facts=facts,
        )

        updates: dict[str, object] = {}
        prov = dict(product.field_provenance)
        if "description" in fields and not product.description:
            updates["description"] = asset.body
            prov["description"] = PROV_GENERATED
        if "short_description" in fields and not product.short_description:
            updates["short_description"] = asset.body[:160]
            prov["short_description"] = PROV_GENERATED
        if "seo_title" in fields and not product.seo_title and (product.title or product.sku):
            updates["seo_title"] = (product.title or product.sku)[:70]
            prov["seo_title"] = PROV_DERIVED
        if "seo_description" in fields and not product.seo_description:
            updates["seo_description"] = asset.body[:160]
            prov["seo_description"] = PROV_GENERATED

        new_product = replace(product, field_provenance=prov, **updates)
        self.store.save_product(new_product)
        self._emit("product.enrich", tenant=tenant, product_id=product_id, updated_fields=list(updates.keys()))
        return {
            "product_id": product_id,
            "asset_version_id": asset.version_id,
            "status": asset.status,
            "missing_facts": list(asset.missing_facts),
            "updated_fields": list(updates.keys()),
        }

    # --- Media association (spec section 14, reuses existing Artifacts) -----

    def associate_media(self, *, tenant_id: str, product_id: str, artifact_ids: tuple[str, ...]) -> dict:
        tenant = require_tenant_id(tenant_id)
        product = self.store.get_product(product_id, tenant_id=tenant)
        if product is None:
            raise ProductIntelError(PRODUCT_NOT_FOUND, "product_not_found")
        verified: list[str] = []
        for aid in artifact_ids:
            if self.artifact_service is None:
                verified.append(aid)
                continue
            try:
                self.artifact_service.get_metadata(tenant_id=tenant, artifact_id=aid)
                verified.append(aid)
            except Exception:
                continue
        new_refs = tuple(dict.fromkeys(list(product.media_refs) + verified))
        new_product = replace(product, media_refs=new_refs)
        self.store.save_product(new_product)
        return {"product_id": product_id, "media_refs": list(new_refs)}

    # --- Export / handoff (spec section 19) ---------------------------------

    def _to_canonical_export(self, p: Product) -> dict:
        return {
            "product_id": p.product_id,
            "sku": p.sku,
            "article": p.article,
            "gtin": p.gtin,
            "mpn": p.mpn,
            "title": p.title,
            "brand": p.brand,
            "category": p.category,
            "category_path": list(p.category_path),
            "description": p.description,
            "short_description": p.short_description,
            "seo_title": p.seo_title,
            "seo_description": p.seo_description,
            "attributes": dict(p.attributes),
            "price": {
                "currency": p.price.currency,
                "purchase_price": str(p.price.purchase_price) if p.price.purchase_price is not None else None,
                "selling_price": str(p.price.selling_price) if p.price.selling_price is not None else None,
            },
            "stock": {
                "quantity": str(p.stock.quantity) if p.stock.quantity is not None else None,
                "availability": p.stock.availability,
            },
            "media_refs": list(p.media_refs),
            "content_refs": list(p.content_refs),
            "validation_state": p.validation_state,
            "matching_state": p.matching_state,
            "field_provenance": dict(p.field_provenance),
        }

    def export_catalog(
        self,
        *,
        tenant_id: str,
        catalog_id: str | None = None,
        as_artifact: bool = False,
        owner_id: str = "",
        conversation_id: str = "",
        request_id: str = "",
    ) -> dict:
        tenant = require_tenant_id(tenant_id)
        products = self.store.list_products(tenant_id=tenant, catalog_id=catalog_id)
        export_rows = [self._to_canonical_export(p) for p in products]
        result = {
            "tenant_id": tenant,
            "catalog_id": catalog_id or f"catalog-{tenant}",
            "count": len(export_rows),
            "schema_version": SCHEMA_VERSION,
            "products": export_rows,
            "exported": False,
        }
        if not as_artifact:
            return result
        if self.artifact_service is None:
            return result
        content = json.dumps(
            {"schema_version": SCHEMA_VERSION, "products": export_rows}, ensure_ascii=False, indent=2
        ).encode("utf-8")
        # ``.json``/``application/json`` are not on the shared artifact
        # allow-list (``artifacts.validation.ALLOWED_EXTENSIONS`` -- Block
        # 3.5.12/15 bounded file security); mirror the established
        # ``content_intel.service.export_asset_as_artifact`` convention of
        # rendering structured output as a bounded ``.txt``/``text/plain``
        # artifact rather than widening that shared allow-list for one
        # caller.
        filename = f"product_catalog_export_{(catalog_id or tenant)[:24]}.txt"
        try:
            rec = self.artifact_service.register_generated(
                tenant_id=tenant,
                owner_id=owner_id,
                filename=filename,
                content=content,
                mime_type="text/plain",
                conversation_id=conversation_id,
                request_id=request_id,
                tool_id="product.export",
            )
        except Exception:
            return result
        public = rec.as_public_dict()
        result["exported"] = True
        result["artifact_id"] = rec.artifact_id
        result["view_url"] = public["view_url"]
        result["download_url"] = public["download_url"]
        return result

    # --- Chat / multi-turn NL dispatch (spec section 20) --------------------

    def execute_nl_request(
        self, *, tenant_id: str, text: str, dataset_id: str = "", catalog_id: str | None = None
    ) -> dict:
        """Single bounded chat-facing entry point (mirrors
        ``data.excel_assistant``): a deterministic keyword dispatcher over
        the already-governed ``product.*`` operations. Panda decides which
        underlying operation to run -- the user never picks a technical
        mode. Ambiguous/ destructive intents fall back to a safe status
        summary rather than guessing.
        """

        tenant = require_tenant_id(tenant_id)
        cid = catalog_id or f"catalog-{tenant}"
        blob = (text or "").lower()

        def has(*subs: str) -> bool:
            return any(s in blob for s in subs)

        if has("дубли", "дубликат", "duplicate"):
            groups = self.find_duplicate_groups(tenant_id=tenant, catalog_id=cid)
            return {
                "operation": "duplicates",
                "catalog_id": cid,
                "groups": [{"product_ids": list(g.product_ids)} for g in groups],
            }

        if has("сопостав", "match"):
            products = self.store.list_products(tenant_id=tenant, catalog_id=cid)
            outcomes = [
                p.product_id for p in products if p.matching_state not in {MATCH_STATE_EXACT, "NEW"}
            ]
            return {
                "operation": "match_summary",
                "catalog_id": cid,
                "total": len(products),
                "unresolved_product_ids": outcomes,
            }

        if has("характеристик", "normalize") and has("нормализ", "normalize"):
            products = self.store.list_products(tenant_id=tenant, catalog_id=cid)
            return {"operation": "normalize", "catalog_id": cid, "normalized_count": len(products)}

        if has("остат", "stock") and has("обнов", "reconcile", "update") and dataset_id:
            return {"operation": "reconcile", **self.reconcile_stock_from_dataset(tenant_id=tenant, dataset_id=dataset_id, catalog_id=cid)}

        if has("карточ", "описани", "content", "seo"):
            products = self.store.list_products(tenant_id=tenant, catalog_id=cid)
            enriched: list[str] = []
            if self.content_intelligence_service is not None:
                for p in products[:20]:
                    try:
                        self.enrich_product_content(tenant_id=tenant, product_id=p.product_id)
                        enriched.append(p.product_id)
                    except ProductIntelError:
                        continue
            return {"operation": "enrich", "catalog_id": cid, "enriched_product_ids": enriched}

        if has("экспорт", "export", "выгруз"):
            return {"operation": "export", **self.export_catalog(tenant_id=tenant, catalog_id=cid)}

        if has("провер", "валид", "validate"):
            results = self.validate_catalog(tenant_id=tenant, catalog_id=cid)
            return {
                "operation": "validate",
                "catalog_id": cid,
                "summary": {
                    "valid": sum(1 for r in results.values() if r.state == "VALID"),
                    "warning": sum(1 for r in results.values() if r.state == "WARNING"),
                    "invalid": sum(1 for r in results.values() if r.state == "INVALID"),
                },
            }

        if dataset_id:
            result = self.import_from_excel_dataset(tenant_id=tenant, dataset_id=dataset_id, catalog_id=cid)
            return {
                "operation": "import",
                "catalog_id": result.catalog_id,
                "created": result.created,
                "updated": result.updated,
                "ambiguous": result.ambiguous,
                "invalid": result.invalid,
                "product_ids": list(result.product_ids),
            }

        products = self.store.list_products(tenant_id=tenant, catalog_id=cid)
        return {"operation": "status", "catalog_id": cid, "count": len(products)}

    def get_product(self, product_id: str, *, tenant_id: str) -> Product | None:
        return self.store.get_product(product_id, tenant_id=require_tenant_id(tenant_id))

    def list_products(self, *, tenant_id: str, catalog_id: str | None = None) -> list[Product]:
        return self.store.list_products(tenant_id=require_tenant_id(tenant_id), catalog_id=catalog_id)
