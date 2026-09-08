"""Block 5.7A — Marketplace Platform (WB/Ozon/Yandex Market) targeted tests.

Covers spec section 34 (A-I): canonical domain, each of the three
adapters through the new platform bridge, governance, idempotency,
sync/diff, and safety (LIVE fail-closed, no secrets, approval contract).
No real marketplace API calls are made anywhere in this file.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from integrations.activation.errors import IntegrationVerificationFailedError
from integrations.activation.models import ENV_FIXTURE, ENV_LIVE
from integrations.activation.service import IntegrationActivationService
from integrations.ozon.catalog import OzonCatalogStore
from integrations.wildberries.catalog import WildberriesCatalogStore
from integrations.yandex_market.catalog import YandexMarketCatalogStore

from marketplace.errors import MarketplaceError
from marketplace.models import (
    CATEGORY_MATCHED,
    CATEGORY_UNMAPPED,
    DIFF_CONFLICT,
    DIFF_IN_SYNC,
    DIFF_INVALID,
    DIFF_MARKETPLACE_NEWER,
    DIFF_MISSING_IN_MARKETPLACE,
    DIFF_MISSING_IN_PANDA,
    DIFF_PANDA_NEWER,
    DIFF_UNMAPPED,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_CONFIRMED,
    ORDER_STATUS_NEW,
    ORDER_STATUS_PROCESSING,
    ORDER_STATUS_UNKNOWN,
    PROVIDER_OZON,
    PROVIDER_WILDBERRIES,
    PROVIDER_YANDEX_MARKET,
    MarketplaceOrder,
)
from marketplace.platform import (
    MarketplaceCategoryMapStore,
    MarketplacePlatform,
    classify_sync_diff,
    normalize_order_status,
    validate_listing_readiness,
)
from product_intel.errors import ProductBatchRequired


def _isolated_service() -> IntegrationActivationService:
    """A fresh ``IntegrationActivationService`` whose WB/Ozon/Yandex Market
    FIXTURE adapters use per-test-isolated catalog stores instead of the
    module-level ``GLOBAL_*_CATALOG`` singletons those adapters default to.

    Real, reproducible cross-test pollution (spec section 37): those
    globals are shared process-wide, so a write in one test file (e.g. this
    one, exercising the real governed price-write path) silently mutated
    state that a pre-existing test elsewhere in the same pytest process
    (``tests/test_real_ozon_integration_closure.py::OzonReadTests::
    test_price_read_semantics`` and its Yandex Market analog) assumed
    pristine. Minimum safe fix scoped to the new code introducing the
    write traffic: give every test in *this* file its own isolated store
    rather than touching the pre-existing test files or adapter defaults.
    """
    svc = IntegrationActivationService()
    svc._wb_fixture._store = WildberriesCatalogStore()  # noqa: SLF001
    svc._ozon_fixture._store = OzonCatalogStore()  # noqa: SLF001
    svc._ym_fixture._store = YandexMarketCatalogStore()  # noqa: SLF001
    return svc


def _activated_connection(svc: IntegrationActivationService, *, tenant_id: str, provider_id: str, environment: str = ENV_FIXTURE, credential_ref: str | None = None):
    conn = svc.configure_connection(
        tenant_id=tenant_id,
        provider_id=provider_id,
        credential_ref=credential_ref or f"secret:{provider_id}-demo",
        environment=environment,
    )
    svc.verify_connection(tenant_id=tenant_id, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant_id, connection_id=conn.connection_id)
    return conn


# =====================================================================
# A. Canonical marketplace domain
# =====================================================================
class CanonicalDomainTests(unittest.TestCase):
    def test_order_status_normalizes_known_and_unknown_safely(self):
        known = normalize_order_status(provider=PROVIDER_WILDBERRIES, raw_status="NEW")
        self.assertEqual(known.canonical_status, ORDER_STATUS_NEW)
        self.assertEqual(known.provider_status, "NEW")

        unknown = normalize_order_status(provider=PROVIDER_WILDBERRIES, raw_status="SOME_FUTURE_STATUS")
        self.assertEqual(unknown.canonical_status, ORDER_STATUS_UNKNOWN)
        self.assertEqual(unknown.provider_status, "SOME_FUTURE_STATUS")

    def test_category_mapping_lookup_unmapped_then_upsert_matched(self):
        store = MarketplaceCategoryMapStore()
        before = store.lookup(tenant_id="tenant-a", provider=PROVIDER_OZON, panda_category="phones")
        self.assertEqual(before.status, CATEGORY_UNMAPPED)
        self.assertEqual(before.external_category_id, "")

        after = store.upsert(
            tenant_id="tenant-a",
            provider=PROVIDER_OZON,
            panda_category="phones",
            external_category_id="oz-cat-phones",
            required_attributes=("color",),
        )
        self.assertEqual(after.status, CATEGORY_MATCHED)
        looked_up = store.lookup(tenant_id="tenant-a", provider=PROVIDER_OZON, panda_category="phones")
        self.assertEqual(looked_up.external_category_id, "oz-cat-phones")
        self.assertEqual(looked_up.required_attributes, ("color",))

    def test_category_mapping_is_tenant_and_provider_scoped(self):
        store = MarketplaceCategoryMapStore()
        store.upsert(tenant_id="tenant-a", provider=PROVIDER_OZON, panda_category="phones", external_category_id="oz-cat-phones")
        other_tenant = store.lookup(tenant_id="tenant-b", provider=PROVIDER_OZON, panda_category="phones")
        other_provider = store.lookup(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, panda_category="phones")
        self.assertEqual(other_tenant.status, CATEGORY_UNMAPPED)
        self.assertEqual(other_provider.status, CATEGORY_UNMAPPED)

    def test_listing_readiness_reports_all_missing_fields(self):
        store = MarketplaceCategoryMapStore()
        unmapped = store.lookup(tenant_id="tenant-a", provider=PROVIDER_OZON, panda_category="phones")
        result = validate_listing_readiness(canonical_product={}, provider=PROVIDER_OZON, category_map=unmapped)
        self.assertFalse(result.ready)
        codes = {issue.code for issue in result.issues}
        self.assertIn("MISSING_PRODUCT_IDENTITY", codes)
        self.assertIn("MISSING_SKU_IDENTITY", codes)
        self.assertIn("CATEGORY_UNMAPPED", codes)
        self.assertIn("MISSING_TITLE", codes)
        self.assertIn("MISSING_PRICE", codes)
        self.assertIn("MISSING_STOCK", codes)

    def test_listing_readiness_ready_when_complete(self):
        store = MarketplaceCategoryMapStore()
        mapped = store.upsert(
            tenant_id="tenant-a", provider=PROVIDER_OZON, panda_category="phones",
            external_category_id="oz-cat-phones", required_attributes=("color",),
        )
        product = {
            "product_id": "p1",
            "sku": "SKU-1",
            "title": "Case",
            "price": {"selling_price": "500"},
            "stock": {"quantity": 5},
            "attributes": {"color": "black"},
        }
        result = validate_listing_readiness(canonical_product=product, provider=PROVIDER_OZON, category_map=mapped)
        self.assertTrue(result.ready)
        self.assertEqual(result.issues, ())

    def test_listing_readiness_missing_required_attribute(self):
        store = MarketplaceCategoryMapStore()
        mapped = store.upsert(
            tenant_id="tenant-a", provider=PROVIDER_OZON, panda_category="phones",
            external_category_id="oz-cat-phones", required_attributes=("color",),
        )
        product = {
            "product_id": "p1", "sku": "SKU-1", "title": "Case",
            "price": {"selling_price": "500"}, "stock": {"quantity": 5},
        }
        result = validate_listing_readiness(canonical_product=product, provider=PROVIDER_OZON, category_map=mapped)
        self.assertFalse(result.ready)
        self.assertEqual(result.issues[0].code, "MISSING_REQUIRED_ATTRIBUTE")

    def test_marketplace_order_references_canonical_identity_not_vendor_id_only(self):
        order = MarketplaceOrder(
            tenant_id="tenant-a", provider=PROVIDER_OZON, account_id="acct-1",
            external_order_id="oz-order-1", status=normalize_order_status(provider=PROVIDER_OZON, raw_status="delivered"),
        )
        self.assertEqual(order.tenant_id, "tenant-a")
        self.assertEqual(order.status.canonical_status, "DELIVERED")


# =====================================================================
# B/C/D. Wildberries / Ozon / Yandex Market adapters (via the platform)
# =====================================================================
class _ProviderContractMixin:
    provider_id: str
    provider_const: str
    sku: str
    warehouse: str

    def setUp(self):
        self.svc = _isolated_service()
        self.conn = _activated_connection(self.svc, tenant_id="tenant-a", provider_id=self.provider_id)
        self.platform = MarketplacePlatform(activation=self.svc, provider=self.provider_const)

    def test_catalog_pagination(self):
        page1 = self.platform.read_catalog_page(tenant_id="tenant-a", connection_id=self.conn.connection_id, page=1)
        self.assertTrue(page1["items"])
        self.assertEqual(page1["page"], 1)
        far = self.platform.read_catalog_page(tenant_id="tenant-a", connection_id=self.conn.connection_id, page=999)
        self.assertEqual(far["items"], [])
        self.assertTrue(far.get("bounded"))

    def test_price_read_and_governed_write_is_idempotent(self):
        before = self.platform.read_price(tenant_id="tenant-a", external_sku=self.sku, connection_id=self.conn.connection_id)
        self.assertGreater(before.amount.amount, Decimal("0"))

        first = self.platform.write_price(
            tenant_id="tenant-a", external_sku=self.sku, amount=Decimal("1234"),
            idempotency_key="price-op-1", approved_write=True, connection_id=self.conn.connection_id,
        )
        self.assertEqual(first["status"], "WRITE_ACCEPTED")
        self.assertFalse(first.get("idempotent"))

        replay = self.platform.write_price(
            tenant_id="tenant-a", external_sku=self.sku, amount=Decimal("1234"),
            idempotency_key="price-op-1", approved_write=True, connection_id=self.conn.connection_id,
        )
        self.assertTrue(replay.get("idempotent"))

    def test_stock_read_and_governed_write_is_idempotent(self):
        stock = self.platform.read_stock(tenant_id="tenant-a", external_sku=self.sku, warehouse=self.warehouse, connection_id=self.conn.connection_id)
        self.assertGreaterEqual(stock.quantity, 0)

        first = self.platform.write_stock(
            tenant_id="tenant-a", external_sku=self.sku, quantity=7, warehouse=self.warehouse,
            idempotency_key="stock-op-1", approved_write=True, connection_id=self.conn.connection_id,
        )
        self.assertEqual(first["status"], "WRITE_ACCEPTED")
        replay = self.platform.write_stock(
            tenant_id="tenant-a", external_sku=self.sku, quantity=7, warehouse=self.warehouse,
            idempotency_key="stock-op-1", approved_write=True, connection_id=self.conn.connection_id,
        )
        self.assertTrue(replay.get("idempotent"))

    def test_orders_read_and_normalized(self):
        orders = self.platform.read_orders(tenant_id="tenant-a", account_id="acct-1", connection_id=self.conn.connection_id)
        self.assertTrue(orders)
        for order in orders:
            self.assertEqual(order.provider, self.provider_const)
            self.assertEqual(order.account_id, "acct-1")
            self.assertIn(order.status.canonical_status, {
                ORDER_STATUS_NEW, ORDER_STATUS_CONFIRMED, ORDER_STATUS_PROCESSING,
                ORDER_STATUS_CANCELLED, ORDER_STATUS_UNKNOWN,
            })
            self.assertTrue(order.items)
            self.assertTrue(order.external_order_id)

    def test_publish_listing_requires_readiness_and_is_governed(self):
        unmapped = self.platform.category_maps.lookup(tenant_id="tenant-a", provider=self.provider_const, panda_category="phones")
        with self.assertRaises(MarketplaceError) as ctx:
            self.platform.publish_listing(
                tenant_id="tenant-a",
                canonical_product={"product_id": "p1", "sku": f"{self.sku}-NEW", "title": "New item"},
                category_map=unmapped, idempotency_key="pub-fail", approved_write=True,
                connection_id=self.conn.connection_id,
            )
        self.assertEqual(ctx.exception.code, "MARKETPLACE_NOT_READY")

        mapped = self.platform.category_maps.upsert(
            tenant_id="tenant-a", provider=self.provider_const, panda_category="phones", external_category_id="ext-phones-1",
        )
        ready_product = {
            "product_id": "p1", "sku": f"{self.sku}-NEW", "title": "New item",
            "price": {"selling_price": "500"}, "stock": {"quantity": 3},
        }
        out = self.platform.publish_listing(
            tenant_id="tenant-a", canonical_product=ready_product, category_map=mapped,
            idempotency_key="pub-ok", approved_write=True, connection_id=self.conn.connection_id,
        )
        # WB publishes synchronously (WRITE_ACCEPTED); Ozon/Yandex Market
        # model an async import/submission pipeline (SUBMITTED) -- a real,
        # explicit provider difference (spec section 4/25), not collapsed.
        self.assertIn(out["status"], {"WRITE_ACCEPTED", "SUBMITTED"})

    def test_unsupported_operation_never_fakes_success(self):
        with self.assertRaises(MarketplaceError) as ctx:
            self.platform._write(
                tenant_id="tenant-a", environment=ENV_FIXTURE,
                payload={"operation": "not_a_real_operation"},
                idempotency_key="bogus-op", approved_write=True, connection_id=self.conn.connection_id,
            )
        self.assertEqual(ctx.exception.code, "MARKETPLACE_CAPABILITY_UNSUPPORTED")


class WildberriesAdapterTests(_ProviderContractMixin, unittest.TestCase):
    provider_id = "wildberries"
    provider_const = PROVIDER_WILDBERRIES
    sku = "WB-SKU-100"
    warehouse = "main"


class OzonAdapterTests(_ProviderContractMixin, unittest.TestCase):
    provider_id = "ozon"
    provider_const = PROVIDER_OZON
    sku = "OZ-SKU-100"
    warehouse = "fbs_main"


class YandexMarketAdapterTests(_ProviderContractMixin, unittest.TestCase):
    provider_id = "yandex_market"
    provider_const = PROVIDER_YANDEX_MARKET
    sku = "YM-SKU-100"
    warehouse = "dbs_main"


# =====================================================================
# E. Governance
# =====================================================================
class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.svc = _isolated_service()
        self.conn = _activated_connection(self.svc, tenant_id="tenant-a", provider_id="ozon")
        self.platform = MarketplacePlatform(activation=self.svc, provider=PROVIDER_OZON)

    def test_write_without_approval_is_denied(self):
        with self.assertRaises(MarketplaceError) as ctx:
            self.platform.write_price(
                tenant_id="tenant-a", external_sku="OZ-SKU-100", amount=Decimal("1"),
                idempotency_key="k", approved_write=False, connection_id=self.conn.connection_id,
            )
        self.assertEqual(ctx.exception.code, "MARKETPLACE_APPROVAL_REQUIRED")

    def test_write_without_idempotency_key_is_denied(self):
        with self.assertRaises(MarketplaceError) as ctx:
            self.platform.write_price(
                tenant_id="tenant-a", external_sku="OZ-SKU-100", amount=Decimal("1"),
                idempotency_key="", approved_write=True, connection_id=self.conn.connection_id,
            )
        self.assertEqual(ctx.exception.code, "MARKETPLACE_APPROVAL_REQUIRED")

    def test_cross_tenant_access_is_denied(self):
        with self.assertRaises(MarketplaceError) as ctx:
            self.platform.read_price(tenant_id="tenant-b", external_sku="OZ-SKU-100", connection_id=self.conn.connection_id)
        self.assertEqual(ctx.exception.code, "MARKETPLACE_ACCESS_DENIED")

    def test_read_uses_governed_capability_and_emits_telemetry(self):
        before = len(self.svc._evidence)  # noqa: SLF001 -- internal event log, test-only inspection
        self.platform.read_catalog_page(tenant_id="tenant-a", connection_id=self.conn.connection_id)
        after = self.svc._evidence  # noqa: SLF001
        self.assertGreater(len(after), before)
        last = after[-1]
        self.assertEqual(last.capability, "marketplace.product")
        self.assertNotIn("secret:", str(last.__dict__))


# =====================================================================
# F. Idempotency (cross-provider, explicit dedicated coverage)
# =====================================================================
class IdempotencyTests(unittest.TestCase):
    def test_repeated_price_write_does_not_duplicate_external_action(self):
        svc = _isolated_service()
        conn = _activated_connection(svc, tenant_id="tenant-a", provider_id="wildberries")
        platform = MarketplacePlatform(activation=svc, provider=PROVIDER_WILDBERRIES)
        for _ in range(3):
            out = platform.write_price(
                tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("1777"),
                idempotency_key="same-key", approved_write=True, connection_id=conn.connection_id,
            )
        self.assertTrue(out.get("idempotent"))
        # a different idempotency key for the same effective change is a
        # distinct external action (adapter-level write_count check).
        out2 = platform.write_price(
            tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("1777"),
            idempotency_key="different-key", approved_write=True, connection_id=conn.connection_id,
        )
        self.assertFalse(out2.get("idempotent"))


# =====================================================================
# G. Sync / diff
# =====================================================================
class SyncDiffTests(unittest.TestCase):
    def test_diff_classification_matrix(self):
        self.assertEqual(classify_sync_diff(panda_state={"value": "1"}, marketplace_state={"value": "1"}), DIFF_IN_SYNC)
        self.assertEqual(classify_sync_diff(panda_state={"value": "1"}, marketplace_state=None), DIFF_MISSING_IN_MARKETPLACE)
        self.assertEqual(classify_sync_diff(panda_state=None, marketplace_state={"value": "1"}), DIFF_MISSING_IN_PANDA)
        self.assertEqual(classify_sync_diff(panda_state=None, marketplace_state=None), DIFF_INVALID)
        self.assertEqual(
            classify_sync_diff(panda_state={"value": "1"}, marketplace_state={"value": "1"}, category_mapped=False), DIFF_UNMAPPED
        )
        self.assertEqual(
            classify_sync_diff(
                panda_state={"value": "1", "updated_at": 2}, marketplace_state={"value": "2", "updated_at": 1},
            ),
            DIFF_PANDA_NEWER,
        )
        self.assertEqual(
            classify_sync_diff(
                panda_state={"value": "1", "updated_at": 1}, marketplace_state={"value": "2", "updated_at": 2},
            ),
            DIFF_MARKETPLACE_NEWER,
        )
        self.assertEqual(classify_sync_diff(panda_state={"value": "1"}, marketplace_state={"value": "2"}), DIFF_CONFLICT)

    def test_diff_price_end_to_end_missing_in_marketplace(self):
        svc = _isolated_service()
        conn = _activated_connection(svc, tenant_id="tenant-a", provider_id="ozon")
        platform = MarketplacePlatform(activation=svc, provider=PROVIDER_OZON)
        state = platform.diff_price(
            tenant_id="tenant-a", external_sku="DOES-NOT-EXIST", panda_amount=Decimal("100"), connection_id=conn.connection_id,
        )
        self.assertEqual(state.diff, DIFF_MISSING_IN_MARKETPLACE)

    def test_diff_price_end_to_end_in_sync(self):
        svc = _isolated_service()
        conn = _activated_connection(svc, tenant_id="tenant-a", provider_id="ozon")
        platform = MarketplacePlatform(activation=svc, provider=PROVIDER_OZON)
        remote = platform.read_price(tenant_id="tenant-a", external_sku="OZ-SKU-100", connection_id=conn.connection_id)
        state = platform.diff_price(
            tenant_id="tenant-a", external_sku="OZ-SKU-100", panda_amount=remote.amount.amount, connection_id=conn.connection_id,
        )
        self.assertEqual(state.diff, DIFF_IN_SYNC)

    def test_bulk_sync_gate_reuses_existing_batch_admission(self):
        svc = _isolated_service()
        platform = MarketplacePlatform(activation=svc, provider=PROVIDER_OZON)
        with self.assertRaises(ProductBatchRequired):
            platform.bulk_sync_gate(item_count=10_000, bulk=False)
        platform.bulk_sync_gate(item_count=10_000, bulk=True)  # does not raise


# =====================================================================
# H. Safety
# =====================================================================
class SafetyTests(unittest.TestCase):
    def test_live_without_configuration_fails_closed_for_all_three_providers(self):
        svc = _isolated_service()
        for provider_id in ("wildberries", "ozon", "yandex_market"):
            conn = svc.configure_connection(
                tenant_id="tenant-a", provider_id=provider_id, credential_ref=f"secret:{provider_id}-live", environment=ENV_LIVE,
            )
            with self.assertRaises(IntegrationVerificationFailedError):
                svc.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)

    def test_unknown_provider_is_rejected_not_silently_accepted(self):
        svc = _isolated_service()
        with self.assertRaises(MarketplaceError) as ctx:
            MarketplacePlatform(activation=svc, provider="SOME_OTHER_MARKETPLACE")
        self.assertEqual(ctx.exception.code, "MARKETPLACE_CAPABILITY_UNSUPPORTED")

    def test_no_secret_value_appears_in_any_normalized_result_or_report(self):
        svc = _isolated_service()
        conn = _activated_connection(svc, tenant_id="tenant-a", provider_id="ozon", credential_ref="secret:ozon-demo-value")
        platform = MarketplacePlatform(activation=svc, provider=PROVIDER_OZON)
        page = platform.read_catalog_page(tenant_id="tenant-a", connection_id=conn.connection_id)
        price = platform.read_price(tenant_id="tenant-a", external_sku="OZ-SKU-100", connection_id=conn.connection_id)
        write = platform.write_price(
            tenant_id="tenant-a", external_sku="OZ-SKU-100", amount=Decimal("1777"),
            idempotency_key="safety-1", approved_write=True, connection_id=conn.connection_id,
        )
        for blob in (str(page), str(price), str(write)):
            self.assertNotIn("ozon-demo-value", blob)

    def test_bulk_write_still_requires_explicit_approval_and_idempotency(self):
        """Mass price/stock changes (spec section 21) go through the same
        governed write path as a single change -- no second approval
        system is introduced; ``approved_write``/``idempotency_key`` are
        still mandatory."""
        svc = _isolated_service()
        conn = _activated_connection(svc, tenant_id="tenant-a", provider_id="ozon")
        platform = MarketplacePlatform(activation=svc, provider=PROVIDER_OZON)
        skus = ["OZ-SKU-100", "OZ-SKU-200"]
        with self.assertRaises(MarketplaceError):
            for sku in skus:
                platform.write_price(
                    tenant_id="tenant-a", external_sku=sku, amount=Decimal("1111"),
                    idempotency_key=f"bulk-{sku}", approved_write=False, connection_id=conn.connection_id,
                )


if __name__ == "__main__":
    unittest.main()
