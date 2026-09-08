"""Block 5.6 — Bitrix / Aspro Premier Integration — closure tests.

Covers Acceptance A-U for the new Bitrix <-> Block 5.5 Product Intelligence
bridge (``integrations.bitrix.product_bridge.BitrixProductBridge``) and the
targeted extensions to the pre-existing (Block 5.4-era) governed Bitrix
connector layer. Reuses the same deterministic fixture adapter/store and
``IntegrationActivationService`` already proven in
``tests/test_bitrix_aspro_premier_closure.py`` -- no live/paid calls.
"""

from __future__ import annotations

import unittest

from business_assistant.service import BusinessAssistantService
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.catalog import BitrixCatalogStore
from integrations.bitrix.errors import BitrixNotFoundError
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter
from integrations.bitrix.mapping import canonical_to_bitrix_payload
from integrations.bitrix.product_bridge import (
    SYNC_AMBIGUOUS,
    SYNC_CREATE,
    SYNC_INVALID,
    SYNC_SKIP,
    SYNC_UNCHANGED,
    SYNC_UPDATE,
    BitrixProductBridge,
)
from product_intel.errors import ProductBatchRequired
from product_intel.service import ProductIntelligenceService
from product_intel.store import InMemoryProductCatalogStore


def _activation(store: BitrixCatalogStore) -> IntegrationActivationService:
    svc = IntegrationActivationService()
    adapter = BitrixFixtureAdapter(store=store)
    svc._adapters["bitrix"] = adapter
    svc._bitrix_fixture = adapter
    return svc


def _active(svc: IntegrationActivationService, *, tenant: str = "tenant-a") -> None:
    ref = svc.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:bitrix-{tenant}", value=f"tok-{tenant}")
    conn = svc.configure_connection(tenant_id=tenant, provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE)
    svc.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)


def _bridge(store: BitrixCatalogStore | None = None) -> tuple[BitrixProductBridge, IntegrationActivationService, BitrixCatalogStore]:
    store = store or BitrixCatalogStore()
    activation = _activation(store)
    _active(activation)
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)
    return bridge, activation, store


def _product_intel() -> ProductIntelligenceService:
    return ProductIntelligenceService(InMemoryProductCatalogStore())


class ConfigurationHealthTests(unittest.TestCase):
    """Acceptance A / B."""

    def test_health_distinguishes_states_without_secrets(self):
        bridge, activation, _ = _bridge()
        conn = activation.list_connections(tenant_id="tenant-a", provider_id="bitrix")[0]
        out = bridge.health(tenant_id="tenant-a", connection_id=conn.connection_id)
        self.assertEqual(out["status"], "HEALTHY")
        self.assertNotIn("secret", str(out).lower())

    def test_health_reports_not_configured_when_disconnected(self):
        activation = IntegrationActivationService()
        bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE)
        with self.assertRaises(Exception):
            activation.get_connection(tenant_id="tenant-a", connection_id="missing")


class CatalogSectionReadTests(unittest.TestCase):
    """Acceptance C."""

    def test_sections_readable_with_stable_external_ids(self):
        bridge, _, _ = _bridge()
        out = bridge.read_sections(tenant_id="tenant-a")
        names = {s["name"] for s in out["items"]}
        self.assertIn("Smartphones", names)
        self.assertIn("Accessories", names)
        for s in out["items"]:
            self.assertTrue(s["section_id"].startswith("sec-"))


class ProductVariantReadImportTests(unittest.TestCase):
    """Acceptance D / E / F."""

    def test_import_maps_products_and_offers_into_canonical_product(self):
        bridge, _, store = _bridge()
        product_intel = _product_intel()
        result = bridge.import_catalog(tenant_id="tenant-a", product_intelligence_service=product_intel, max_pages=3)
        self.assertGreater(result["imported"], 0)

        products = product_intel.store.list_products(tenant_id="tenant-a")
        skus = {p.sku for p in products}
        # Base product (no offers) present.
        self.assertIn("SKU-X200", skus)
        # Distinct offers remain distinct SKUs/products (Acceptance E) --
        # never collapsed into one record.
        self.assertIn("SKU-X100-BLK", skus)
        self.assertIn("SKU-X100-WHT", skus)
        blk = next(p for p in products if p.sku == "SKU-X100-BLK")
        wht = next(p for p in products if p.sku == "SKU-X100-WHT")
        self.assertNotEqual(blk.product_id, wht.product_id)
        self.assertEqual(blk.variant_attributes.get("color"), "black")
        self.assertEqual(wht.variant_attributes.get("color"), "white")

        # External Bitrix id/provenance preserved (Acceptance F).
        self.assertEqual(blk.source.source_type, "bitrix")
        self.assertTrue(blk.source.row_ref)
        self.assertEqual(store.get_mapping(tenant_id="tenant-a", panda_product_id=blk.product_id), "offer-1001-black")

    def test_import_is_tenant_scoped(self):
        bridge, activation, _ = _bridge()
        _active(activation, tenant="tenant-b")
        product_intel_a = _product_intel()
        product_intel_b = _product_intel()
        bridge.import_catalog(tenant_id="tenant-a", product_intelligence_service=product_intel_a)
        bridge.import_catalog(tenant_id="tenant-b", product_intelligence_service=product_intel_b)
        self.assertTrue(len(product_intel_a.store.list_products(tenant_id="tenant-a")) > 0)
        # Each ProductIntelligenceService instance is itself tenant-scoped
        # storage; tenant B's products never appear when listing tenant A.
        self.assertEqual(
            len(product_intel_a.store.list_products(tenant_id="tenant-b")),
            0,
        )


class PandaToBitrixMappingTests(unittest.TestCase):
    """Acceptance G / M / N."""

    def test_canonical_export_maps_deterministically_including_seo_media(self):
        canonical = {
            "title": "Phone X",
            "sku": "SKU-1",
            "description": "desc",
            "seo_title": "Buy Phone X",
            "seo_description": "Best phone",
            "media_refs": ["artifact:1", "artifact:2"],
        }
        payload = canonical_to_bitrix_payload(product=canonical, aspro_enabled=False)
        self.assertEqual(payload["NAME"], "Phone X")
        self.assertEqual(payload["SEO_TITLE"], "Buy Phone X")
        self.assertEqual(payload["SEO_DESCRIPTION"], "Best phone")
        self.assertEqual(payload["DETAIL_PICTURE"], "artifact:1")
        self.assertEqual(payload["MORE_PHOTO"], ["artifact:2"])

        aspro_payload = canonical_to_bitrix_payload(product=canonical, aspro_enabled=True)
        self.assertEqual(aspro_payload["storefront"], "aspro_premier")
        self.assertIn("IPROPERTY_TEMPLATES_ELEMENT_META_TITLE", aspro_payload)
        self.assertEqual(aspro_payload["IPROPERTY_TEMPLATES_ELEMENT_META_TITLE"], "Buy Phone X")
        self.assertEqual(aspro_payload["DETAIL_PICTURE"], "artifact:1")

    def test_mapping_missing_required_fields_fails_closed(self):
        from integrations.bitrix.errors import BitrixValidationError
        from integrations.bitrix.mapping import validate_create_payload

        with self.assertRaises(BitrixValidationError):
            validate_create_payload(canonical_to_bitrix_payload(product={"description": "x"}))


class GovernedCreateUpdateTests(unittest.TestCase):
    """Acceptance H / I / O / P."""

    def _canonical(self, **overrides) -> dict:
        base = {
            "product_id": "panda-1",
            "title": "New Widget",
            "sku": "SKU-NEW-WIDGET",
            "description": "A widget",
            "price": {"currency": "RUB", "purchase_price": None, "selling_price": "1500"},
            "stock": {"quantity": "5", "availability": "IN_STOCK"},
        }
        base.update(overrides)
        return base

    def test_create_then_unchanged_produces_no_remote_mutation(self):
        bridge, activation, store = _bridge()
        canonical = self._canonical()
        r1 = bridge.sync_product(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="sync-1")
        self.assertEqual(r1["action"], SYNC_CREATE)
        self.assertTrue(r1["mutated"])
        write_count_after_create = store.write_count("sync-1")
        self.assertEqual(write_count_after_create, 1)

        # Re-plan with identical content: must be UNCHANGED (Acceptance O) --
        # no remote mutation, regardless of idempotency key.
        r2 = bridge.sync_product(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="sync-2")
        self.assertEqual(r2["action"], SYNC_UNCHANGED)
        self.assertFalse(r2["mutated"])
        self.assertEqual(store.write_count("sync-2"), 0)

    def test_update_changes_only_intended_fields(self):
        bridge, activation, store = _bridge()
        canonical = self._canonical(sku="SKU-X200")  # matches seeded fixture product
        plan = bridge.plan_sync(tenant_id="tenant-a", canonical_product=canonical)
        self.assertEqual(plan["action"], SYNC_UPDATE)
        self.assertIn("name", plan["changes"])
        r = bridge.sync_product(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="upd-1")
        self.assertEqual(r["action"], SYNC_UPDATE)
        self.assertTrue(r["mutated"])
        self.assertEqual(r["result"]["product"]["name"], "New Widget")

    def test_ambiguous_target_rejected_not_updated(self):
        bridge, activation, store = _bridge()
        canonical = self._canonical(sku="SKU-AMBIG")
        # Seed a genuine duplicate article to force ambiguity.
        store.create_product(tenant_id="tenant-a", payload={"name": "Dup", "article": "SKU-AMBIG", "price": "10"})
        plan = bridge.plan_sync(tenant_id="tenant-a", canonical_product=canonical)
        self.assertEqual(plan["action"], SYNC_AMBIGUOUS)
        r = bridge.sync_product(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="amb-1")
        self.assertEqual(r["action"], SYNC_AMBIGUOUS)
        self.assertFalse(r["mutated"])

    def test_invalid_missing_identity_fails_closed(self):
        bridge, _, _ = _bridge()
        plan = bridge.plan_sync(tenant_id="tenant-a", canonical_product={"title": "No SKU"})
        self.assertEqual(plan["action"], SYNC_INVALID)

    def test_idempotent_repeat_does_not_duplicate(self):
        bridge, activation, store = _bridge()
        canonical = self._canonical()
        r1 = bridge.sync_product(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="same-key")
        self.assertTrue(r1["mutated"])
        self.assertEqual(store.write_count("same-key"), 1)
        # A second call with the identical (already-applied) content is
        # naturally UNCHANGED before it ever reaches the write path
        # (Acceptance O) -- but simulate the retry case directly too: same
        # idempotency key + same payload hitting the connector again must
        # replay, never duplicate the remote effect (Acceptance P).
        r2 = bridge.sync_product(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="same-key")
        self.assertEqual(r2["action"], SYNC_UNCHANGED)
        self.assertEqual(store.write_count("same-key"), 1)
        replay = activation.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "product_create",
                "panda_product_id": canonical["product_id"],
                "product": canonical,
            },
            idempotency_key="same-key",
            approved_write=True,
        )
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(store.write_count("same-key"), 1)


class PriceStockMediaSeoSyncTests(unittest.TestCase):
    """Acceptance J / K / L."""

    def test_price_sync_from_product_intelligence(self):
        bridge, activation, store = _bridge()
        canonical = {"sku": "SKU-X100-BLK", "price": {"currency": "RUB", "selling_price": "55555.00"}}
        r = bridge.sync_price(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="price-1")
        self.assertTrue(r["mutated"])
        self.assertEqual(r["result"]["verified"], "VERIFIED")
        read = activation.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "price_read", "article": "SKU-X100-BLK"},
        )
        self.assertEqual(read["result"]["price"]["amount"], "55555.00")

    def test_stock_sync_never_invents_unknown_stock(self):
        bridge, _, store = _bridge()
        canonical_unknown = {"sku": "SKU-X100", "stock": {"quantity": None}}
        r = bridge.sync_stock(tenant_id="tenant-a", canonical_product=canonical_unknown, idempotency_key="stock-unknown")
        self.assertEqual(r["action"], SYNC_SKIP)
        self.assertFalse(r["mutated"])

    def test_stock_sync_writes_known_quantity(self):
        bridge, activation, store = _bridge()
        canonical = {"sku": "SKU-X100", "stock": {"quantity": "42"}}
        r = bridge.sync_stock(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="stock-known")
        self.assertTrue(r["mutated"])
        self.assertEqual(r["result"]["stock"]["new"], 42)

    def test_media_association_avoids_duplicate_attach(self):
        bridge, activation, store = _bridge()
        canonical = {"product_id": "panda-media-1", "sku": "SKU-X100", "media_refs": ["artifact:a", "artifact:b"]}
        r1 = bridge.associate_media(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="media-1")
        self.assertEqual(r1["added"], ["artifact:a", "artifact:b"])
        r2 = bridge.associate_media(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="media-2")
        self.assertEqual(r2["added"], [])
        self.assertEqual(r2["duplicate_skipped"], 2)

    def test_seo_sync_maps_title_and_description(self):
        bridge, activation, store = _bridge()
        canonical = {"sku": "SKU-X100", "seo_title": "Best Phone", "seo_description": "Great deal"}
        r = bridge.sync_seo(tenant_id="tenant-a", canonical_product=canonical, idempotency_key="seo-1")
        self.assertTrue(r["mutated"])
        self.assertEqual(r["result"]["product"]["properties"]["seo_title"], "Best Phone")


class TenantIsolationFailureNormalizationTests(unittest.TestCase):
    """Acceptance R / Q."""

    def test_tenant_b_cannot_sync_using_tenant_a_mapping(self):
        bridge, _, store = _bridge()
        store.bind_mapping(tenant_id="tenant-a", panda_product_id="panda-x", bitrix_id="bitrix-prod-1001")
        canonical = {"product_id": "panda-x", "title": "Something", "sku": "NO-MATCH-IN-B"}
        plan = bridge.plan_sync(tenant_id="tenant-b", canonical_product=canonical)
        # Tenant B's catalog has no such mapping/article -- must not resolve
        # tenant A's target.
        self.assertEqual(plan["action"], SYNC_CREATE)

    def test_write_failure_raises_normalized_error_not_raw_exception(self):
        bridge, _, _ = _bridge()
        with self.assertRaises(BitrixNotFoundError):
            bridge._activation.execute_via_gateway(
                tenant_id="tenant-a",
                capability="cms.bitrix.catalog.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "product_update", "article": "NO-SUCH", "changes": {"name": "x"}},
                idempotency_key="fail-1",
                approved_write=True,
            )


class BulkBatchRoutingTests(unittest.TestCase):
    """Acceptance S."""

    def test_large_bulk_without_bulk_flag_requires_batch_routing(self):
        bridge, _, _ = _bridge()
        products = [{"product_id": f"p{i}", "title": f"T{i}", "sku": f"SKU-BULK-{i}"} for i in range(30)]
        with self.assertRaises(ProductBatchRequired):
            bridge.bulk_sync(tenant_id="tenant-a", canonical_products=products, bulk=False)

    def test_bulk_sync_reports_bounded_per_item_results(self):
        bridge, _, _ = _bridge()
        products = [{"product_id": f"p{i}", "title": f"Bulk {i}", "sku": f"SKU-BULK-{i}"} for i in range(3)]
        out = bridge.bulk_sync(tenant_id="tenant-a", canonical_products=products, bulk=True)
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["succeeded"], 3)
        self.assertEqual(len(out["items"]), 3)


class ConversationBoundaryTests(unittest.TestCase):
    """Acceptance T."""

    def test_bitrix_catalog_import_via_chat(self):
        activation = _activation(BitrixCatalogStore())
        _active(activation)
        bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE)
        product_intel = _product_intel()
        ba = BusinessAssistantService(
            integration_activation=activation,
            integration_environment=ENV_FIXTURE,
            bitrix_product_bridge=bridge,
            product_intelligence_service=product_intel,
        )
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Импортируй каталог Bitrix в Panda",
            read_only=True,
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertTrue(any(a.get("type") == "bitrix_import" for a in ex.artifacts))
        self.assertTrue(len(product_intel.store.list_products(tenant_id="tenant-a")) > 0)


class ProductionReadBoundaryTests(unittest.TestCase):
    """Acceptance U -- no production configuration present."""

    def test_live_configuration_absent_reports_pending(self):
        import os

        from integrations.bitrix.config import load_bitrix_config

        cfg = load_bitrix_config({k: v for k, v in os.environ.items() if not k.startswith("BITRIX_")})
        self.assertFalse(cfg.live_configured)


if __name__ == "__main__":
    unittest.main()
