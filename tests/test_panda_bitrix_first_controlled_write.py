"""PANDA — first controlled production Bitrix/Aspro product write.

Covers ``business_assistant.controlled_bitrix_write``: a single-product,
approval-gated, idempotent, read-back-verified governed create on top of
the pre-existing ``BitrixProductBridge`` / ``IntegrationActivationService``
machinery (Block 5.4/5.6) -- no new write path, no bypass of the existing
approval/idempotency gate.

Cursor performs ZERO real production mutations while implementing/testing
this: every test below runs exclusively against the deterministic FIXTURE
adapter/store (``integrations.bitrix.fixture_adapter.BitrixFixtureAdapter``
+ ``integrations.bitrix.catalog.BitrixCatalogStore``), never a live
connection. The real first production write is performed later, manually,
by the OWNER through the Panda UI.
"""

from __future__ import annotations

import unittest

from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.catalog import BitrixCatalogStore
from integrations.bitrix.errors import BitrixWriteVerificationFailedError
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter
from integrations.bitrix.product_bridge import BitrixProductBridge

from business_assistant.controlled_bitrix_write import (
    STATUS_AMBIGUOUS,
    STATUS_APPROVAL_REQUIRED,
    STATUS_EXISTING_PRODUCT_FOUND,
    STATUS_REQUIRES_APPROVAL,
    STATUS_WRITE_FAILED,
    STATUS_WRITE_VERIFIED,
    ControlledWriteBatchNotAllowedError,
    SingleProductWriteRequest,
    assert_single_row,
    build_write_request_from_row,
    execute_single_product_write,
    prepare_single_product_write,
)

TARGET_SKU = "32LQ63006LA.ARUG"
TARGET_TITLE = "Телевизор LG 32LQ63006LA.ARUG"
TARGET_EAN = "8806096259955"
TARGET_CATEGORY = "CE"
TARGET_BRAND = "LG"
TARGET_PURCHASE_PRICE = "22513.70"
TARGET_RETAIL_PRICE = "29990"


def _bridge(store: BitrixCatalogStore | None = None) -> tuple[BitrixProductBridge, IntegrationActivationService, BitrixCatalogStore]:
    store = store or BitrixCatalogStore()
    activation = IntegrationActivationService()
    adapter = BitrixFixtureAdapter(store=store)
    activation._adapters["bitrix"] = adapter
    ref = activation.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:bitrix-tenant-a", value="tok")
    conn = activation.configure_connection(
        tenant_id="tenant-a", provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
    )
    activation.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    activation.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)
    return bridge, activation, store


def _request(**overrides) -> SingleProductWriteRequest:
    base = dict(
        tenant_id="tenant-a",
        title=TARGET_TITLE,
        sku=TARGET_SKU,
        retail_price=TARGET_RETAIL_PRICE,
        ean=TARGET_EAN,
        category_source=TARGET_CATEGORY,
        brand=TARGET_BRAND,
        purchase_price=TARGET_PURCHASE_PRICE,
    )
    base.update(overrides)
    return SingleProductWriteRequest(**base)


class ExactlyOneProductWriteTests(unittest.TestCase):
    def test_approved_write_creates_exactly_one_product(self):
        bridge, _, store = _bridge()
        before = len(store.catalog("tenant-a"))
        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(), approved=True
        )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertTrue(result["mutated"])
        after = len(store.catalog("tenant-a"))
        self.assertEqual(after - before, 1)


class ApprovalGateTests(unittest.TestCase):
    def test_explicit_approval_required_before_any_write(self):
        bridge, _, store = _bridge()
        preview = prepare_single_product_write(bridge, tenant_id="tenant-a", request=_request())
        self.assertEqual(preview["status"], STATUS_REQUIRES_APPROVAL)
        # The preview itself must show the exact target product and retail
        # price BEFORE any execution/approval.
        self.assertEqual(preview["target_product"]["sku"], TARGET_SKU)
        self.assertEqual(preview["target_product"]["title"], TARGET_TITLE)
        self.assertEqual(preview["retail_price"]["amount"], TARGET_RETAIL_PRICE)

    def test_no_approval_means_zero_mutation(self):
        bridge, _, store = _bridge()
        before = len(store.catalog("tenant-a"))
        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(), approved=False
        )
        self.assertEqual(result["status"], STATUS_APPROVAL_REQUIRED)
        self.assertFalse(result["mutated"])
        self.assertEqual(len(store.catalog("tenant-a")), before)

    def test_approved_write_denied_without_idempotency_key_never_reaches_adapter(self):
        # Defensive proof that even if a caller bypassed this module and
        # called the underlying gateway directly with approved_write=True
        # but no idempotency key, the pre-existing Block 5.4 gate itself
        # still refuses -- this module never needs to duplicate that check.
        bridge, activation, store = _bridge()
        from integrations.activation.errors import IntegrationWriteDeniedError

        with self.assertRaises(IntegrationWriteDeniedError):
            activation.execute_via_gateway(
                tenant_id="tenant-a",
                capability="cms.bitrix.catalog.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "product_create", "product": {"title": "X", "sku": "Y"}},
                idempotency_key="",
                approved_write=True,
            )


class PriceSeparationTests(unittest.TestCase):
    def test_purchase_price_and_retail_price_remain_separate(self):
        bridge, _, store = _bridge()
        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(), approved=True
        )
        self.assertFalse(result["purchase_price_written"])
        self.assertEqual(result["purchase_price_source"], TARGET_PURCHASE_PRICE)
        stored = store.catalog("tenant-a")[result["bitrix_product_id"]]
        # Only ONE price is ever stored, and it is the retail price.
        self.assertEqual(len(stored["prices"]), 1)
        self.assertEqual(stored["prices"][0]["amount"], TARGET_RETAIL_PRICE)
        # Purchase price never appears anywhere in the persisted product.
        self.assertNotIn(TARGET_PURCHASE_PRICE, str(stored))

    def test_retail_price_goes_only_to_verified_selling_price_destination(self):
        bridge, activation, store = _bridge()
        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(), approved=True
        )
        read = activation.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "price_read", "article": TARGET_SKU},
        )
        self.assertEqual(read["result"]["price"]["amount"], TARGET_RETAIL_PRICE)
        self.assertEqual(read["result"]["price_type"], "RETAIL")

    def test_canonical_payload_never_carries_a_purchase_price_key(self):
        bridge, _, _ = _bridge()
        preview = prepare_single_product_write(bridge, tenant_id="tenant-a", request=_request())
        canonical = preview["canonical_payload"]
        self.assertNotIn("purchase_price", canonical.get("price", {}))
        self.assertNotIn("purchase_price", canonical)


class NoGuessedPropertiesTests(unittest.TestCase):
    def test_ean_and_purchase_price_and_category_reported_but_not_written(self):
        bridge, _, store = _bridge()
        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(), approved=True
        )
        fields_not_written = {item["field"] for item in result["not_written"]}
        self.assertEqual(fields_not_written, {"ean", "purchase_price", "category"})
        stored = store.catalog("tenant-a")[result["bitrix_product_id"]]
        # No invented/guessed property code holds EAN, purchase price, or
        # category -- only the verified "brand" property is populated.
        self.assertEqual(set(stored["properties"].keys()), {"brand"})
        self.assertEqual(stored["properties"]["brand"], TARGET_BRAND)
        self.assertNotIn(TARGET_EAN, str(stored["properties"]))

    def test_missing_retail_price_is_unresolved_not_guessed(self):
        bridge, _, _ = _bridge()
        preview = prepare_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(retail_price="")
        )
        self.assertEqual(preview["status"], "UNRESOLVED")
        self.assertEqual(preview["reason"], "missing_or_invalid_retail_price")


class DuplicateProtectionTests(unittest.TestCase):
    def test_existing_product_with_same_sku_blocks_create_and_requires_decision(self):
        bridge, _, store = _bridge()
        store.create_product(
            tenant_id="tenant-a",
            payload={"name": "Already there", "article": TARGET_SKU, "price": "1000"},
        )
        before = len(store.catalog("tenant-a"))
        preview = prepare_single_product_write(bridge, tenant_id="tenant-a", request=_request())
        self.assertEqual(preview["status"], STATUS_EXISTING_PRODUCT_FOUND)
        self.assertTrue(preview["requires_separate_decision"])

        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(), approved=True
        )
        self.assertEqual(result["status"], STATUS_EXISTING_PRODUCT_FOUND)
        self.assertFalse(result["mutated"])
        self.assertEqual(len(store.catalog("tenant-a")), before)

    def test_ambiguous_target_also_blocks_create(self):
        bridge, _, store = _bridge()
        store.create_product(tenant_id="tenant-a", payload={"name": "Dup 1", "article": TARGET_SKU, "price": "10"})
        store.create_product(tenant_id="tenant-a", payload={"name": "Dup 2", "article": TARGET_SKU, "price": "10"})
        preview = prepare_single_product_write(bridge, tenant_id="tenant-a", request=_request())
        self.assertEqual(preview["status"], STATUS_AMBIGUOUS)
        result = execute_single_product_write(bridge, tenant_id="tenant-a", request=_request(), approved=True)
        self.assertFalse(result["mutated"])


class IdempotentRetryTests(unittest.TestCase):
    def test_repeated_approved_call_with_same_key_does_not_duplicate(self):
        bridge, _, store = _bridge()
        req = _request()
        r1 = execute_single_product_write(bridge, tenant_id="tenant-a", request=req, approved=True)
        self.assertEqual(r1["status"], STATUS_WRITE_VERIFIED)
        count_after_first = len(store.catalog("tenant-a"))

        # Same request -> same deterministic idempotency key by default.
        self.assertEqual(r1["idempotency_key"], _default_key_for(req, tenant_id="tenant-a"))

        r2 = execute_single_product_write(
            bridge,
            tenant_id="tenant-a",
            request=req,
            approved=True,
            idempotency_key=r1["idempotency_key"],
        )
        # The retry replays the exact cached write, never creating a second
        # product for the same request.
        self.assertEqual(len(store.catalog("tenant-a")), count_after_first)
        self.assertEqual(store.write_count(r1["idempotency_key"]), 1)


def _default_key_for(request: SingleProductWriteRequest, *, tenant_id: str) -> str:
    from business_assistant.controlled_bitrix_write import _default_idempotency_key

    return _default_idempotency_key(tenant_id, request)


class ReadBackVerificationTests(unittest.TestCase):
    def test_successful_write_is_independently_read_back_and_verified(self):
        bridge, _, store = _bridge()
        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(), approved=True
        )
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        read_back = result["read_back"]
        self.assertTrue(read_back["matches"])
        self.assertEqual(read_back["observed"]["name"], TARGET_TITLE)
        self.assertEqual(read_back["observed"]["active"], False)
        self.assertEqual(read_back["mismatched_fields"], [])

        # Independently confirm the read-back actually queried Bitrix again
        # (not merely echoing the write's own embedded copy).
        fresh = bridge.read_product(tenant_id="tenant-a", bitrix_id=result["bitrix_product_id"])
        self.assertEqual(fresh["name"], TARGET_TITLE)


class WriteFailureIsolationTests(unittest.TestCase):
    def test_write_verification_failure_does_not_mutate_other_products(self):
        bridge, activation, store = _bridge()
        before = {k: dict(v) for k, v in store.catalog("tenant-a").items()}
        # Force the underlying provider call itself to fail (outage), the
        # same normalized-error path a real transient Bitrix failure would
        # take -- not merely an internal "verified" flag flip.
        activation._adapters["bitrix"].state.unavailable = True

        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_request(), approved=True
        )
        self.assertEqual(result["status"], STATUS_WRITE_FAILED)
        self.assertFalse(result["mutated"])

        after = store.catalog("tenant-a")
        # No pre-existing seeded product was touched, and no new product
        # was left behind by the failed attempt.
        self.assertEqual(set(after.keys()), set(before.keys()))
        for bid, prod in before.items():
            self.assertEqual(after[bid], prod)


class BatchNotAllowedTests(unittest.TestCase):
    def test_multiple_rows_raise_batch_not_allowed(self):
        with self.assertRaises(ControlledWriteBatchNotAllowedError):
            assert_single_row([{"sku": "A"}, {"sku": "B"}])

    def test_zero_rows_raise_batch_not_allowed(self):
        with self.assertRaises(ControlledWriteBatchNotAllowedError):
            assert_single_row([])

    def test_exactly_one_row_passes(self):
        row = {"sku": "A"}
        self.assertEqual(assert_single_row([row]), row)


class BuildRequestFromRealXlsxRowTests(unittest.TestCase):
    """Closes the loop: a real ingested XLSX row builds the exact write
    request, dynamically, from the dataset's own inferred schema."""

    def test_row_from_ingested_workbook_builds_matching_request(self):
        import io

        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["sku", "product_name", "ean", "category", "brand", "Предоплата,\nЦена с НДС"])
        ws.append(["SAM-A54", "Galaxy A54", "1111111111111", "Phones", "Samsung", "18000.00"])
        ws.append([TARGET_SKU, TARGET_TITLE, TARGET_EAN, TARGET_CATEGORY, TARGET_BRAND, TARGET_PURCHASE_PRICE])
        buf = io.BytesIO()
        wb.save(buf)

        svc = DataIntelligenceService(InMemoryDatasetStore())
        ing = svc.ingest(buf.getvalue(), filename="LG_TV.xlsx", tenant_id="tenant-a", enqueue_large=False)
        desc = svc.store.get_dataset(ing["dataset_id"], tenant_id="tenant-a")
        table = desc.tables[0]
        rows = svc.store.get_rows(ing["dataset_id"], tenant_id="tenant-a", table_id=table.table_id)
        target_row = assert_single_row([r for r in rows if r.get("sku") == TARGET_SKU])

        req = build_write_request_from_row(
            target_row, table.columns, tenant_id="tenant-a", retail_price=TARGET_RETAIL_PRICE
        )
        self.assertEqual(req.title, TARGET_TITLE)
        self.assertEqual(req.sku, TARGET_SKU)
        self.assertEqual(req.ean, TARGET_EAN)
        self.assertEqual(req.category_source, TARGET_CATEGORY)
        self.assertEqual(req.brand, TARGET_BRAND)
        self.assertEqual(req.purchase_price, TARGET_PURCHASE_PRICE)
        self.assertEqual(req.retail_price, TARGET_RETAIL_PRICE)

        bridge, _, store = _bridge()
        result = execute_single_product_write(bridge, tenant_id="tenant-a", request=req, approved=True)
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)


if __name__ == "__main__":
    unittest.main()
