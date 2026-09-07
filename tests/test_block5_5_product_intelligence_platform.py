"""PANDA — BLOCK 5.5 E-commerce / Product Intelligence — targeted tests +
required deterministic acceptance (A-O).

Covers the new canonical, vendor-neutral Product/Catalog Intelligence layer
(``product_intel/``): domain model, field mapping/normalization (reusing
Block 5.1 ``data_intel``), deterministic-first matching/dedupe (reusing
Block 5.2 ``acquisition``/``data_intel.product_match``), validation,
reconciliation, content enrichment (reusing Block 5.3 ``content_intel``),
media/artifact handoff, tenant isolation, the governed Block 5.4
``ToolGateway`` contract, workload classification, canonical export, a fake
future-connector proof, and the ``FAMILY_PRODUCT`` multi-turn chat
continuation wiring in ``business_assistant/action_continuation.py`` /
``conversation_gateway.py``.

Uses only local fixtures / in-memory stores (no live network, no paid model
calls). Mirrors the Block 5.1/5.2/5.3/5.4 test conventions.
"""

from __future__ import annotations

import io
import json
import unittest
from decimal import Decimal

from openpyxl import Workbook

from acquisition.identifiers import validate_ean
from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from autonomy.capabilities import CAP_FILESYSTEM_READ, CAP_FILESYSTEM_WRITE, CapabilitySet
from autonomy.models import utc_now
from business_assistant.action_continuation import (
    ASK_CLARIFICATION,
    CALL_TOOL,
    CONTINUE_ACTIVE_TASK,
    FAMILY_EXCEL,
    FAMILY_PRODUCT,
    NEW_TASK,
    PRODUCT_CONTRACT,
    TOOL_PRODUCT_CATALOG_ASSIST,
    ActiveTask,
    ActiveTaskStore,
    STATUS_DRAFT,
    detect_family,
    format_tool_user_text,
    resolve_action_turn,
)
from content_intel.service import ContentIntelligenceService
from content_intel.sqlite_store import SqliteContentStore
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from product_intel.errors import ProductBatchRequired, ProductCrossTenantError
from product_intel.matching import match_against_catalog, match_pair
from product_intel.normalize import (
    normalize_attribute_key,
    normalize_attribute_value,
    normalize_brand,
    normalize_currency,
    normalize_price,
    normalize_stock_quantity,
    normalize_title,
)
from product_intel.planner import LARGE_SYNC_ITEMS, assert_sync_product_allowed
from product_intel.platform_models import (
    MATCH_STATE_AMBIGUOUS,
    MATCH_STATE_EXACT,
    MATCH_STATE_HIGH_CONFIDENCE,
    MATCH_STATE_NO_MATCH,
    VALIDATION_INVALID,
    VALIDATION_VALID,
    VALIDATION_WARNING,
    PriceInfo,
    Product,
    SourceReference,
    StockInfo,
)
from product_intel.service import ProductIntelligenceService, build_product_from_row
from product_intel.store import InMemoryProductCatalogStore
from product_intel.validation import validate_catalog, validate_product
from tools.errors import ToolArgumentInvalidError
from tools.gateway import ToolGateway
from tools.models import TOOL_STATUS_SUCCEEDED, ToolRequest
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

# A GS1 checksum-valid EAN-13 (deterministically verified, no live lookup).
VALID_EAN = "4006381333931"


def _svc(*, with_content=False, with_artifacts=False, with_data_intel=True):
    store = InMemoryProductCatalogStore()
    data_svc = DataIntelligenceService(InMemoryDatasetStore()) if with_data_intel else None
    content_svc = ContentIntelligenceService(SqliteContentStore(":memory:")) if with_content else None
    artifact_svc = ArtifactService(store=InMemoryArtifactStore()) if with_artifacts else None
    return ProductIntelligenceService(
        store,
        data_intelligence_service=data_svc,
        content_intelligence_service=content_svc,
        artifact_service=artifact_svc,
    )


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Canonical domain model
# --------------------------------------------------------------------------
class CanonicalModelTests(unittest.TestCase):
    def test_product_requires_tenant_and_supports_partial_record(self):
        # Spec section 3: "The model must support partial product records
        # safely" -- only title supplied, everything else defaults cleanly.
        product = Product(product_id="p1", tenant_id="tenant-a", title="Widget")
        self.assertEqual(product.tenant_id, "tenant-a")
        self.assertEqual(product.sku, "")
        self.assertEqual(product.price.selling_price, None)
        self.assertEqual(product.validation_state, "UNVALIDATED")
        self.assertEqual(product.matching_state, "NEW")

    def test_product_rejects_missing_tenant(self):
        with self.assertRaises(Exception):
            Product(product_id="p1", tenant_id="", title="x")

    def test_price_and_stock_are_immutable_value_objects(self):
        price = PriceInfo(currency="RUB", selling_price=Decimal("100"))
        with self.assertRaises(Exception):
            price.selling_price = Decimal("200")  # frozen dataclass

    def test_source_reference_sanitizes_raw_snapshot(self):
        ref = SourceReference(source_type="excel", raw_snapshot={"sku": "A1", "n": 1})
        self.assertEqual(dict(ref.raw_snapshot)["sku"], "A1")


# --------------------------------------------------------------------------
# Normalization (Acceptance B)
# --------------------------------------------------------------------------
class NormalizationTests(unittest.TestCase):
    def test_title_normalization_is_whitespace_and_case_insensitive(self):
        self.assertEqual(normalize_title("  Samsung   Galaxy  "), normalize_title("samsung galaxy"))

    def test_brand_normalization(self):
        self.assertEqual(normalize_brand("  SAMSUNG "), "samsung")

    def test_price_normalization_handles_ru_decimal_comma(self):
        self.assertEqual(normalize_price("1 234,50"), Decimal("1234.50"))

    def test_price_normalization_rejects_negative(self):
        self.assertIsNone(normalize_price("-100"))

    def test_currency_alias_normalization(self):
        self.assertEqual(normalize_currency("руб"), "RUB")
        self.assertEqual(normalize_currency("$"), "USD")

    def test_stock_quantity_normalization(self):
        self.assertEqual(normalize_stock_quantity("15"), Decimal("15"))

    def test_attribute_key_and_value_normalization(self):
        self.assertEqual(normalize_attribute_key("Цвет"), "цвет")
        self.assertEqual(normalize_attribute_value("  Black   Onyx "), "Black Onyx")


# --------------------------------------------------------------------------
# Ingestion + field mapping (Acceptance A) + normalization preserving
# provenance (Acceptance B)
# --------------------------------------------------------------------------
class IngestionAndMappingTests(unittest.TestCase):
    def test_payload_rows_map_to_canonical_products_with_provenance(self):
        svc = _svc(with_data_intel=False)
        rows = [
            {
                "sku": "SKU-1",
                "product_name": "Samsung Galaxy 128GB",
                "brand": "Samsung",
                "category": "Phones",
                "price": "49990",
                "stock": "12",
            }
        ]
        result = svc.import_rows(tenant_id="tenant-a", rows=rows, source_type="payload")
        self.assertEqual(result.created, 1)
        self.assertEqual(result.invalid, 0)
        product = svc.get_product(result.product_ids[0], tenant_id="tenant-a")
        self.assertEqual(product.sku, "SKU-1")
        self.assertEqual(product.normalized_brand, "samsung")
        self.assertEqual(product.price.selling_price, Decimal("49990"))
        self.assertEqual(product.field_provenance["sku"], "SOURCE")
        self.assertEqual(product.source.source_type, "payload")
        # Original raw source row must remain traceable/recoverable.
        self.assertEqual(product.source.raw_snapshot["product_name"], "Samsung Galaxy 128GB")

    def test_excel_ingestion_via_block51_data_intel(self):
        """Acceptance A: representative XLSX -> canonical tenant-scoped products."""

        data_svc = DataIntelligenceService(InMemoryDatasetStore())
        svc = ProductIntelligenceService(InMemoryProductCatalogStore(), data_intelligence_service=data_svc)
        xlsx = _xlsx_bytes(
            [
                ["Артикул", "Наименование", "Бренд", "Цена", "Остаток", "Штрихкод"],
                ["A-100", "Стул офисный", "ChairCo", "3500", "10", "4006381333931"],
                ["A-101", "Стол офисный", "ChairCo", "7200", "4", ""],
            ]
        )
        ingest = data_svc.ingest(xlsx, filename="price.xlsx", tenant_id="tenant-a")
        result = svc.import_from_excel_dataset(tenant_id="tenant-a", dataset_id=ingest["dataset_id"])
        self.assertEqual(result.total_rows, 2)
        self.assertEqual(result.created, 2)
        products = svc.list_products(tenant_id="tenant-a")
        skus = {p.sku for p in products}
        self.assertEqual(skus, {"A-100", "A-101"})
        chair = next(p for p in products if p.sku == "A-100")
        self.assertEqual(chair.gtin, VALID_EAN)
        self.assertEqual(chair.price.selling_price, Decimal("3500"))

    def test_ambiguous_row_with_no_identity_returns_structured_ambiguity(self):
        # Spec section 5: "If required mapping cannot be determined safely:
        # return structured ambiguity/clarification instead of guessing."
        svc = _svc(with_data_intel=False)
        rows = [{"unrelated_column": "???"}]
        result = svc.import_rows(tenant_id="tenant-a", rows=rows, source_type="payload")
        self.assertEqual(result.ambiguous, 1)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.details[0]["status"], "AMBIGUOUS_MAPPING")

    def test_build_product_from_row_returns_none_for_empty_identity(self):
        product, reason = build_product_from_row({"foo": "bar"}, tenant_id="tenant-a")
        self.assertIsNone(product)
        self.assertEqual(reason, "AMBIGUOUS_MAPPING")

    def test_messy_source_fields_normalized_without_destroying_source(self):
        # Acceptance B: messy but valid fields normalized deterministically;
        # the raw source row remains intact in provenance.
        svc = _svc(with_data_intel=False)
        rows = [
            {
                "sku": "sku_007",
                "product_name": "  Стул   Офисный  ",
                "brand": "  ChairCo ",
                "price": "3 500,00",
                "currency": "руб",
                "stock": "7",
            }
        ]
        result = svc.import_rows(tenant_id="tenant-a", rows=rows, source_type="payload")
        product = svc.get_product(result.product_ids[0], tenant_id="tenant-a")
        self.assertEqual(product.sku, "SKU-007")  # normalized, not destroyed
        self.assertEqual(product.normalized_brand, "chairco")
        self.assertEqual(product.price.selling_price, Decimal("3500.00"))
        self.assertEqual(product.price.currency, "RUB")
        self.assertEqual(product.source.raw_snapshot["sku"], "sku_007")  # original preserved


# --------------------------------------------------------------------------
# Matching (Acceptance C, D)
# --------------------------------------------------------------------------
class MatchingTests(unittest.TestCase):
    def test_exact_match_by_valid_gtin(self):
        left = {"product_id": "p1", "gtin": VALID_EAN, "brand": "Samsung", "title": "Galaxy A"}
        right = {"product_id": "p2", "gtin": VALID_EAN, "brand": "Samsung", "title": "Galaxy A"}
        outcome = match_pair(left, right)
        self.assertEqual(outcome.state, MATCH_STATE_EXACT)
        self.assertEqual(outcome.matched_product_id, "p2")

    def test_exact_match_by_sku(self):
        left = {"product_id": "p1", "sku": "ABC-1", "brand": "X"}
        right = {"product_id": "p2", "sku": "ABC-1", "brand": "X"}
        outcome = match_pair(left, right)
        self.assertIn(outcome.state, {MATCH_STATE_EXACT, MATCH_STATE_HIGH_CONFIDENCE})

    def test_ambiguous_match_never_silently_merges(self):
        # Same brand + same normalized title, but NO hard identifiers at all --
        # similarity alone must never resolve to EXACT/HIGH_CONFIDENCE.
        left = {"product_id": "p1", "brand": "Acme", "title": "Widget Pro"}
        right = {"product_id": "p2", "brand": "Acme", "title": "Widget Pro"}
        outcome = match_pair(left, right)
        self.assertEqual(outcome.state, MATCH_STATE_AMBIGUOUS)
        self.assertNotEqual(outcome.matched_product_id, "p2")
        self.assertTrue(outcome.candidates)

    def test_conflicting_identifiers_never_merge(self):
        left = {"product_id": "p1", "gtin": VALID_EAN, "brand": "X", "title": "A"}
        other_ean = "4006381333930"  # digits present but different value
        right = {"product_id": "p2", "gtin": other_ean, "brand": "X", "title": "A"}
        outcome = match_pair(left, right)
        self.assertNotEqual(outcome.state, MATCH_STATE_EXACT)
        self.assertNotEqual(outcome.matched_product_id, "p2")

    def test_match_against_catalog_prefers_exact_over_ambiguous(self):
        candidate = {"product_id": "new", "gtin": VALID_EAN, "brand": "Samsung", "title": "Galaxy A"}
        catalog = [
            {"product_id": "same-name-no-id", "brand": "Samsung", "title": "Galaxy A"},
            {"product_id": "exact-gtin", "gtin": VALID_EAN, "brand": "Samsung", "title": "Galaxy A"},
        ]
        outcome = match_against_catalog(candidate, catalog)
        self.assertEqual(outcome.state, MATCH_STATE_EXACT)
        self.assertEqual(outcome.matched_product_id, "exact-gtin")


# --------------------------------------------------------------------------
# Variant safety + dedupe (Acceptance E)
# --------------------------------------------------------------------------
class VariantSafetyAndDedupeTests(unittest.TestCase):
    def test_legitimate_variants_remain_distinct_products(self):
        svc = _svc(with_data_intel=False)
        rows = [
            {"sku": "PHN-128", "product_name": "Samsung Phone 128 GB", "brand": "Samsung"},
            {"sku": "PHN-256", "product_name": "Samsung Phone 256 GB", "brand": "Samsung"},
        ]
        result = svc.import_rows(tenant_id="tenant-a", rows=rows, source_type="payload")
        self.assertEqual(result.created, 2)
        self.assertEqual(len(set(result.product_ids)), 2)

    def test_duplicate_groups_do_not_collapse_variants(self):
        svc = _svc(with_data_intel=False)
        rows = [
            {"sku": "PHN-128", "product_name": "Samsung Phone 128 GB", "brand": "Samsung"},
            {"sku": "PHN-256", "product_name": "Samsung Phone 256 GB", "brand": "Samsung"},
        ]
        svc.import_rows(tenant_id="tenant-a", rows=rows, source_type="payload")
        groups = svc.find_duplicate_groups(tenant_id="tenant-a")
        self.assertEqual(groups, [])

    def test_duplicate_groups_found_for_true_exact_identifier_duplicates(self):
        svc = _svc(with_data_intel=False)
        # Two rows sharing the SAME GTIN but different SKUs -- a genuine
        # cross-listing duplicate, not a legitimate variant.
        rows = [
            {"sku": "SKU-A", "product_name": "Widget", "brand": "Acme", "ean": VALID_EAN},
        ]
        svc.import_rows(tenant_id="tenant-a", rows=rows, source_type="payload")
        # Force a second product with the SAME gtin via direct store save
        # (import dedupes on gtin at ingest time, so a raw store insert is
        # used here to exercise find_duplicate_groups on two distinct records).
        from dataclasses import replace as _replace

        first = svc.list_products(tenant_id="tenant-a")[0]
        clone = _replace(first, product_id="clone-1", sku="SKU-B")
        svc.store.save_product(clone)
        groups = svc.find_duplicate_groups(tenant_id="tenant-a")
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0].product_ids), {first.product_id, "clone-1"})


# --------------------------------------------------------------------------
# Reconciliation (Acceptance F, reuses Block 5.1)
# --------------------------------------------------------------------------
class ReconciliationTests(unittest.TestCase):
    def test_stock_reconciliation_updates_catalog_from_second_dataset(self):
        data_svc = DataIntelligenceService(InMemoryDatasetStore())
        svc = ProductIntelligenceService(InMemoryProductCatalogStore(), data_intelligence_service=data_svc)
        svc.import_rows(
            tenant_id="tenant-a",
            rows=[{"sku": "SKU-1", "product_name": "Widget", "brand": "Acme", "stock": "5"}],
            source_type="payload",
        )
        stock_xlsx = _xlsx_bytes(
            [
                ["Артикул", "Остаток"],
                ["SKU-1", "40"],
            ]
        )
        ingest = data_svc.ingest(stock_xlsx, filename="stock.xlsx", tenant_id="tenant-a")
        report = svc.reconcile_stock_from_dataset(tenant_id="tenant-a", dataset_id=ingest["dataset_id"])
        self.assertGreaterEqual(report["updated_count"], 1)
        updated = svc.get_product(report["updated_product_ids"][0], tenant_id="tenant-a")
        self.assertEqual(updated.stock.quantity, Decimal("40"))


# --------------------------------------------------------------------------
# Validation (Acceptance G)
# --------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def test_missing_identity_is_invalid(self):
        product = Product(product_id="p1", tenant_id="tenant-a")
        result = validate_product(product)
        self.assertEqual(result.state, VALIDATION_INVALID)
        self.assertTrue(any(i.code == "MISSING_IDENTITY" for i in result.issues))

    def test_invalid_barcode_flagged(self):
        product = Product(product_id="p1", tenant_id="tenant-a", title="X", gtin="1234567890123")
        result = validate_product(product)
        self.assertEqual(result.state, VALIDATION_INVALID)
        self.assertTrue(any(i.code == "INVALID_BARCODE" for i in result.issues))

    def test_negative_stock_flagged(self):
        product = Product(
            product_id="p1",
            tenant_id="tenant-a",
            title="X",
            stock=StockInfo(quantity=Decimal("-5")),
        )
        result = validate_product(product)
        self.assertTrue(any(i.code == "NEGATIVE_STOCK" for i in result.issues))

    def test_valid_product_has_no_issues(self):
        product = Product(
            product_id="p1",
            tenant_id="tenant-a",
            title="Widget",
            sku="SKU-1",
            gtin=VALID_EAN,
            price=PriceInfo(currency="RUB", selling_price=Decimal("100")),
            stock=StockInfo(quantity=Decimal("5")),
        )
        result = validate_product(product)
        self.assertEqual(result.state, VALIDATION_VALID)

    def test_catalog_validation_flags_duplicate_sku(self):
        p1 = Product(product_id="p1", tenant_id="tenant-a", title="A", sku="DUP")
        p2 = Product(product_id="p2", tenant_id="tenant-a", title="B", sku="DUP")
        results = validate_catalog([p1, p2])
        self.assertTrue(any(i.code == "DUPLICATE_SKU" for i in results["p1"].issues))
        self.assertTrue(any(i.code == "DUPLICATE_SKU" for i in results["p2"].issues))

    def test_catalog_validation_flags_duplicate_barcode(self):
        p1 = Product(product_id="p1", tenant_id="tenant-a", title="A", gtin=VALID_EAN)
        p2 = Product(product_id="p2", tenant_id="tenant-a", title="B", gtin=VALID_EAN)
        results = validate_catalog([p1, p2])
        self.assertTrue(any(i.code == "DUPLICATE_BARCODE" for i in results["p1"].issues))

    def test_service_validate_catalog_marks_import_result_invalid(self):
        svc = _svc(with_data_intel=False)
        result = svc.import_rows(
            tenant_id="tenant-a",
            rows=[{"sku": "SKU-1", "product_name": "Widget", "stock": "-3"}],
            source_type="payload",
        )
        self.assertEqual(result.invalid, 1)


# --------------------------------------------------------------------------
# Content enrichment (Acceptance H, reuses Block 5.3)
# --------------------------------------------------------------------------
class ContentEnrichmentTests(unittest.TestCase):
    def test_enrichment_uses_content_intelligence_and_is_grounded(self):
        svc = _svc(with_content=True, with_data_intel=False)
        result = svc.import_rows(
            tenant_id="tenant-a",
            rows=[
                {
                    "sku": "SKU-1",
                    "product_name": "Widget",
                    "brand": "Acme",
                    "price": "100",
                    "stock": "5",
                }
            ],
            source_type="payload",
        )
        product_id = result.product_ids[0]
        outcome = svc.enrich_product_content(tenant_id="tenant-a", product_id=product_id)
        self.assertIn("description", outcome["updated_fields"])
        product = svc.get_product(product_id, tenant_id="tenant-a")
        self.assertTrue(product.description)
        self.assertEqual(product.field_provenance["description"], "GENERATED")

    def test_enrichment_does_not_invent_missing_facts(self):
        # No warranty fact supplied anywhere -- generator must not fabricate it.
        svc = _svc(with_content=True, with_data_intel=False)
        result = svc.import_rows(
            tenant_id="tenant-a",
            rows=[{"sku": "SKU-1", "product_name": "Widget"}],
            source_type="payload",
        )
        outcome = svc.enrich_product_content(tenant_id="tenant-a", product_id=result.product_ids[0])
        self.assertIn("warranty", outcome["missing_facts"])

    def test_enrichment_unavailable_without_content_service(self):
        svc = _svc(with_content=False, with_data_intel=False)
        result = svc.import_rows(
            tenant_id="tenant-a",
            rows=[{"sku": "SKU-1", "product_name": "Widget"}],
            source_type="payload",
        )
        from product_intel.errors import ProductIntelError

        with self.assertRaises(ProductIntelError):
            svc.enrich_product_content(tenant_id="tenant-a", product_id=result.product_ids[0])


# --------------------------------------------------------------------------
# Media / artifact handoff (Acceptance I)
# --------------------------------------------------------------------------
class MediaAssociationTests(unittest.TestCase):
    def test_associate_existing_artifact_as_product_media(self):
        artifact_svc = ArtifactService(store=InMemoryArtifactStore())
        rec = artifact_svc.register_upload(
            tenant_id="tenant-a", owner_id="u1", filename="photo.png", content=b"\x89PNG\r\n", mime_type="image/png"
        )
        svc = _svc(with_data_intel=False)
        svc.artifact_service = artifact_svc
        result = svc.import_rows(
            tenant_id="tenant-a", rows=[{"sku": "SKU-1", "product_name": "Widget"}], source_type="payload"
        )
        out = svc.associate_media(
            tenant_id="tenant-a", product_id=result.product_ids[0], artifact_ids=(rec.artifact_id,)
        )
        self.assertEqual(out["media_refs"], [rec.artifact_id])

    def test_associate_unknown_artifact_is_dropped_not_fabricated(self):
        artifact_svc = ArtifactService(store=InMemoryArtifactStore())
        svc = _svc(with_data_intel=False)
        svc.artifact_service = artifact_svc
        result = svc.import_rows(
            tenant_id="tenant-a", rows=[{"sku": "SKU-1", "product_name": "Widget"}], source_type="payload"
        )
        out = svc.associate_media(
            tenant_id="tenant-a", product_id=result.product_ids[0], artifact_ids=("does-not-exist",)
        )
        self.assertEqual(out["media_refs"], [])


# --------------------------------------------------------------------------
# Tenant isolation (Acceptance J)
# --------------------------------------------------------------------------
class TenantIsolationTests(unittest.TestCase):
    def test_tenant_b_cannot_read_tenant_a_product(self):
        svc = _svc(with_data_intel=False)
        result = svc.import_rows(
            tenant_id="tenant-a", rows=[{"sku": "SKU-1", "product_name": "Widget"}], source_type="payload"
        )
        self.assertIsNone(svc.get_product(result.product_ids[0], tenant_id="tenant-b"))

    def test_tenant_b_catalog_listing_excludes_tenant_a(self):
        svc = _svc(with_data_intel=False)
        svc.import_rows(tenant_id="tenant-a", rows=[{"sku": "SKU-1", "product_name": "A"}], source_type="payload")
        svc.import_rows(tenant_id="tenant-b", rows=[{"sku": "SKU-2", "product_name": "B"}], source_type="payload")
        a_products = svc.list_products(tenant_id="tenant-a")
        b_products = svc.list_products(tenant_id="tenant-b")
        self.assertEqual({p.sku for p in a_products}, {"SKU-1"})
        self.assertEqual({p.sku for p in b_products}, {"SKU-2"})

    def test_access_policy_denies_cross_tenant(self):
        from product_intel.access import ProductAccessPolicy

        policy = ProductAccessPolicy()
        self.assertFalse(policy.allow(requesting_tenant="tenant-a", target_tenant="tenant-b"))
        with self.assertRaises(ProductCrossTenantError):
            policy.require(requesting_tenant="tenant-a", target_tenant="tenant-b")


# --------------------------------------------------------------------------
# Governed Tool Contract (Acceptance K, Block 5.4)
# --------------------------------------------------------------------------
async def _read(adapter, tool_id, operation, tenant_id="tenant-a", **args):
    req = ToolRequest(
        request_id="r",
        workflow_id="w",
        task_id="t",
        tool_id=tool_id,
        operation=operation,
        arguments=args,
        tenant_id=tenant_id,
    )
    return await adapter.execute_read(req, {})


class GovernedToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_and_get_through_adapter(self):
        svc = _svc(with_data_intel=False)
        from product_intel.tools import ProductIntelToolAdapter

        adapter = ProductIntelToolAdapter(svc)
        result = await _read(
            adapter,
            "product.import",
            "import",
            rows=[{"sku": "SKU-1", "product_name": "Widget"}],
        )
        self.assertEqual(result["created"], 1)
        product_id = result["product_ids"][0]
        got = await _read(adapter, "product.get", "get", product_id=product_id)
        self.assertTrue(got["found"])

    async def test_unknown_operation_raises_not_found(self):
        from product_intel.tools import ProductIntelToolAdapter

        adapter = ProductIntelToolAdapter(_svc(with_data_intel=False))
        from tools.errors import ToolNotFoundError

        with self.assertRaises(ToolNotFoundError):
            await _read(adapter, "product.import", "not_a_real_op")

    async def test_full_governed_gateway_path_succeeds(self):
        """Representative product operation through the real
        ToolRegistry -> ToolGateway -> ProductIntelToolAdapter chain with
        correct capability/result behavior."""

        registry = ToolRegistry()
        product_svc = _svc(with_data_intel=False)
        register_platform_tools(registry, product_intelligence_service=product_svc)
        gateway = ToolGateway(registry=registry, register_search=False)
        result = await gateway.invoke(
            ToolRequest(
                request_id="r1",
                workflow_id="wf",
                task_id="t",
                tenant_id="tenant-a",
                tool_id="product.import",
                operation="import",
                arguments={"rows": [{"sku": "SKU-1", "product_name": "Widget"}]},
                requested_capabilities=(CAP_FILESYSTEM_WRITE,),
            ),
            capabilities=CapabilitySet(
                subject_id="tenant-a", capabilities=(CAP_FILESYSTEM_WRITE,), issued_at=utc_now()
            ),
        )
        self.assertEqual(result.status, TOOL_STATUS_SUCCEEDED)
        self.assertTrue(result.success)
        self.assertEqual(result.data["created"], 1)

    async def test_missing_capability_denied_on_governed_path(self):
        registry = ToolRegistry()
        register_platform_tools(registry, product_intelligence_service=_svc(with_data_intel=False))
        gateway = ToolGateway(registry=registry, register_search=False)
        result = await gateway.invoke(
            ToolRequest(
                request_id="r2",
                workflow_id="wf",
                task_id="t",
                tenant_id="tenant-a",
                tool_id="product.import",
                operation="import",
                arguments={"rows": [{"sku": "SKU-1", "product_name": "Widget"}]},
                requested_capabilities=(),
            ),
            capabilities=CapabilitySet(subject_id="tenant-a", capabilities=(), issued_at=utc_now()),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_tool_capability")

    async def test_disabled_when_no_service_wired(self):
        registry = ToolRegistry()
        out = register_platform_tools(registry)  # no product_intelligence_service
        desc = registry.get("product.import")
        self.assertFalse(desc.enabled)
        self.assertIn("product_intel", out["adapters"])

    async def test_validate_and_duplicates_through_adapter(self):
        svc = _svc(with_data_intel=False)
        from product_intel.tools import ProductIntelToolAdapter

        adapter = ProductIntelToolAdapter(svc)
        await _read(
            adapter,
            "product.import",
            "import",
            rows=[
                {"sku": "SKU-1", "product_name": "Widget", "stock": "-1"},
                {"sku": "SKU-2", "product_name": "Widget 2", "gtin": VALID_EAN},
            ],
        )
        validated = await _read(adapter, "product.validate", "validate")
        self.assertGreaterEqual(validated["summary"]["invalid"], 1)
        dup = await _read(adapter, "product.duplicates", "duplicates")
        self.assertEqual(dup["groups"], [])


# --------------------------------------------------------------------------
# Workload classification / batch routing (Acceptance L, reuses Block 3)
# --------------------------------------------------------------------------
class WorkloadClassificationTests(unittest.TestCase):
    def test_small_operation_runs_sync(self):
        assert_sync_product_allowed(item_count=1, bulk=False)  # no raise

    def test_large_operation_requires_batch(self):
        with self.assertRaises(ProductBatchRequired):
            assert_sync_product_allowed(item_count=LARGE_SYNC_ITEMS, bulk=False)

    def test_large_operation_allowed_when_bulk_flagged(self):
        assert_sync_product_allowed(item_count=LARGE_SYNC_ITEMS, bulk=True)  # no raise

    def test_service_import_rows_raises_batch_required_for_large_sync_job(self):
        svc = _svc(with_data_intel=False)
        rows = [{"sku": f"SKU-{i}", "product_name": f"Item {i}"} for i in range(LARGE_SYNC_ITEMS)]
        with self.assertRaises(ProductBatchRequired):
            svc.import_rows(tenant_id="tenant-a", rows=rows, source_type="payload", bulk=False)


class WorkloadClassificationAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_surfaces_batch_required(self):
        from product_intel.tools import ProductIntelToolAdapter

        adapter = ProductIntelToolAdapter(_svc(with_data_intel=False))
        rows = [{"sku": f"SKU-{i}", "product_name": f"Item {i}"} for i in range(LARGE_SYNC_ITEMS)]
        with self.assertRaises(ToolArgumentInvalidError):
            await _read(adapter, "product.import", "import", rows=rows)


# --------------------------------------------------------------------------
# Canonical export / handoff (Acceptance M)
# --------------------------------------------------------------------------
class ExportHandoffTests(unittest.TestCase):
    def test_structured_export_is_vendor_neutral(self):
        svc = _svc(with_data_intel=False)
        svc.import_rows(
            tenant_id="tenant-a",
            rows=[{"sku": "SKU-1", "product_name": "Widget", "brand": "Acme", "price": "10"}],
            source_type="payload",
        )
        out = svc.export_catalog(tenant_id="tenant-a")
        self.assertEqual(out["count"], 1)
        row = out["products"][0]
        self.assertEqual(row["sku"], "SKU-1")
        self.assertIn("schema_version", out)
        self.assertFalse(out["exported"])

    def test_artifact_export_registers_through_canonical_artifact_service(self):
        artifact_svc = ArtifactService(store=InMemoryArtifactStore())
        svc = _svc(with_data_intel=False)
        svc.artifact_service = artifact_svc
        svc.import_rows(
            tenant_id="tenant-a", rows=[{"sku": "SKU-1", "product_name": "Widget"}], source_type="payload"
        )
        out = svc.export_catalog(tenant_id="tenant-a", as_artifact=True, owner_id="u1")
        self.assertTrue(out["exported"])
        self.assertIn("artifact_id", out)
        rec = artifact_svc.get_metadata(tenant_id="tenant-a", artifact_id=out["artifact_id"])
        # .json/application/json are not on the shared artifact allow-list
        # (artifacts.validation.ALLOWED_EXTENSIONS); the export mirrors
        # content_intel's established .txt/text-plain artifact convention.
        self.assertEqual(rec.mime_type, "text/plain")
        content = json.loads(artifact_svc.get_blob(tenant_id="tenant-a", artifact_id=out["artifact_id"])[1])
        self.assertEqual(content["products"][0]["sku"], "SKU-1")


# --------------------------------------------------------------------------
# Future connector compatibility (Acceptance N) — proves no vendor logic
# leaked into product_intel itself.
# --------------------------------------------------------------------------
class FakeVendorConnector:
    """Illustrative future adapter: maps the canonical export -> a made-up
    vendor schema. Lives entirely OUTSIDE product_intel -- this test module
    only, never imported by product_intel itself."""

    @staticmethod
    def to_vendor_payload(canonical_row: dict) -> dict:
        return {
            "vendor_sku": canonical_row["sku"],
            "vendor_name": canonical_row["title"],
            "vendor_price": canonical_row["price"]["selling_price"],
        }


class FutureConnectorCompatibilityTests(unittest.TestCase):
    def test_fake_connector_can_consume_canonical_export(self):
        svc = _svc(with_data_intel=False)
        svc.import_rows(
            tenant_id="tenant-a",
            rows=[{"sku": "SKU-1", "product_name": "Widget", "price": "10"}],
            source_type="payload",
        )
        out = svc.export_catalog(tenant_id="tenant-a")
        vendor_payload = FakeVendorConnector.to_vendor_payload(out["products"][0])
        self.assertEqual(vendor_payload["vendor_sku"], "SKU-1")
        self.assertEqual(vendor_payload["vendor_price"], "10")

    def test_no_vendor_specific_identifiers_in_product_intel_source(self):
        # Static proof that no Bitrix/marketplace/1C-specific business logic
        # leaked into the canonical Product Intelligence module (spec 31).
        import inspect

        import product_intel.platform_models as platform_models
        import product_intel.service as service_mod

        source = inspect.getsource(platform_models) + inspect.getsource(service_mod)
        for forbidden in ("bitrix", "aspro", "wildberries", "ozon", "yandex_market", "1c_"):
            self.assertNotIn(forbidden, source.lower())


# --------------------------------------------------------------------------
# Multi-turn chat continuation (Acceptance O)
# --------------------------------------------------------------------------
class ChatContinuationTests(unittest.TestCase):
    def test_detect_family_for_explicit_catalog_verb(self):
        self.assertEqual(detect_family("Собери каталог товаров из этого файла", None), FAMILY_PRODUCT)

    def test_detect_family_for_product_intent_stems(self):
        self.assertEqual(detect_family("Найди дубли по артикулам", None), FAMILY_PRODUCT)

    def test_first_turn_without_attachment_or_context_asks_for_file(self):
        store = ActiveTaskStore()
        decision = resolve_action_turn(
            "Собери каталог товаров",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            store=store,
            spreadsheet_attachment_count=0,
        )
        self.assertEqual(decision.decision, ASK_CLARIFICATION)
        self.assertEqual(decision.task.family, FAMILY_PRODUCT)

    def test_first_turn_with_attachment_calls_governed_tool(self):
        store = ActiveTaskStore()

        class _FakeDescriptor:
            enabled = True

        class _FakeGateway:
            def get_tool(self, tool_id):
                return _FakeDescriptor()

        decision = resolve_action_turn(
            "Собери каталог товаров из прайса",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            store=store,
            gateway=_FakeGateway(),
            spreadsheet_attachment_count=1,
        )
        self.assertEqual(decision.decision, CALL_TOOL)
        self.assertEqual(decision.tool_id, TOOL_PRODUCT_CATALOG_ASSIST)
        self.assertNotIn("dataset_id", decision.arguments)  # new attachment: no stale dataset_id

    def test_followup_reuses_catalog_without_reattachment(self):
        # Acceptance O: once a prior turn resolved a catalog_id, a follow-up
        # ("Найди дубли") must NOT be asked to re-attach the file.
        store = ActiveTaskStore()
        task = ActiveTask(
            task_id="t1",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            family=FAMILY_PRODUCT,
            tool_id=PRODUCT_CONTRACT.tool_id,
            operation=PRODUCT_CONTRACT.operation,
            goal="Собери каталог",
            parameters={"dataset_id": "", "catalog_id": "catalog-tenant-a"},
            artifact_type=PRODUCT_CONTRACT.artifact_type,
            status="COMPLETED",
        )
        store.put(task)

        class _FakeDescriptor:
            enabled = True

        class _FakeGateway:
            def get_tool(self, tool_id):
                return _FakeDescriptor()

        decision = resolve_action_turn(
            "Найди дубли по артикулам",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            store=store,
            gateway=_FakeGateway(),
            spreadsheet_attachment_count=0,
        )
        self.assertEqual(decision.decision, CALL_TOOL)
        self.assertEqual(decision.arguments.get("catalog_id"), "catalog-tenant-a")
        self.assertEqual(decision.continuation, CONTINUE_ACTIVE_TASK)

    def test_prior_excel_dataset_is_inherited_as_product_source(self):
        # "Загрузи прайс" (Excel) then "Собери из него каталог" (Product)
        # must not force a re-upload -- the prior FAMILY_EXCEL dataset_id is
        # a legitimate existing-context source.
        store = ActiveTaskStore()
        excel_task = ActiveTask(
            task_id="t0",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            family=FAMILY_EXCEL,
            tool_id="data.excel_assistant",
            operation="assist",
            goal="Загрузи прайс",
            parameters={"dataset_id": "ds-123"},
            status="COMPLETED",
        )
        store.put(excel_task)

        class _FakeDescriptor:
            enabled = True

        class _FakeGateway:
            def get_tool(self, tool_id):
                return _FakeDescriptor()

        decision = resolve_action_turn(
            "Собери из него каталог товаров",
            tenant_id="tenant-a",
            owner_id="u1",
            conversation_id="c1",
            store=store,
            gateway=_FakeGateway(),
            spreadsheet_attachment_count=0,
        )
        self.assertEqual(decision.decision, CALL_TOOL)
        self.assertEqual(decision.arguments.get("dataset_id"), "ds-123")

    def test_format_tool_user_text_for_import_and_export(self):
        text = format_tool_user_text(
            family=FAMILY_PRODUCT,
            data={"operation": "import", "created": 3, "updated": 1, "ambiguous": 0},
            success=True,
        )
        self.assertIn("3", text)
        text2 = format_tool_user_text(
            family=FAMILY_PRODUCT,
            data={"operation": "export", "view_url": "/x/view"},
            success=True,
        )
        self.assertIn("/x/view", text2)


class ProductAdapterAttachmentIngestionTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the discovered defect: ``product.catalog_assist``
    previously ignored a freshly attached spreadsheet entirely (never called
    ``DataIntelligenceService.ingest``), silently no-op'ing "Собери каталог
    товаров" turns that included a new attachment instead of an existing
    ``dataset_id``. Fixed in ``product_intel/tools.py``."""

    async def test_assist_ingests_fresh_spreadsheet_attachment(self):
        data_svc = DataIntelligenceService(InMemoryDatasetStore())
        artifact_svc = ArtifactService(store=InMemoryArtifactStore())
        product_svc = ProductIntelligenceService(
            InMemoryProductCatalogStore(),
            data_intelligence_service=data_svc,
            artifact_service=artifact_svc,
        )
        xlsx = _xlsx_bytes(
            [
                ["Артикул", "Наименование", "Цена"],
                ["A-1", "Товар 1", "100"],
                ["A-2", "Товар 2", "200"],
            ]
        )
        rec = artifact_svc.register_upload(
            tenant_id="tenant-a", owner_id="u1", filename="price.xlsx", content=xlsx
        )
        from product_intel.tools import ProductIntelToolAdapter

        adapter = ProductIntelToolAdapter(product_svc)
        result = await _read(
            adapter,
            "product.catalog_assist",
            "assist",
            text="Собери каталог товаров",
            attachment_refs=[{"kind": "spreadsheet", "artifact_id": rec.artifact_id, "filename": "price.xlsx"}],
        )
        self.assertEqual(result["operation"], "import")
        self.assertEqual(result["created"], 2)
        products = product_svc.list_products(tenant_id="tenant-a")
        self.assertEqual(len(products), 2)


# --------------------------------------------------------------------------
# Bootstrap wiring sanity
# --------------------------------------------------------------------------
class BootstrapWiringTests(unittest.TestCase):
    def test_all_product_descriptors_registered(self):
        registry = ToolRegistry()
        register_platform_tools(registry, product_intelligence_service=_svc(with_data_intel=False))
        for tool_id in (
            "product.import",
            "product.match",
            "product.duplicates",
            "product.validate",
            "product.enrich",
            "product.reconcile",
            "product.associate_media",
            "product.export",
            "product.get",
            "product.catalog_assist",
        ):
            desc = registry.get(tool_id)
            self.assertIsNotNone(desc, f"missing descriptor: {tool_id}")
            self.assertTrue(desc.enabled)


if __name__ == "__main__":
    unittest.main()
