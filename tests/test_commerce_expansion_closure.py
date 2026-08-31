"""E-commerce / Product Platform — applied expansion closure tests."""

from __future__ import annotations

import unittest
from decimal import Decimal

from commerce.product_platform.aspro import fixture_aspro_premier_profile, map_product_to_bitrix_payload
from commerce.product_platform.cart import create_cart, revalidate_checkout
from commerce.product_platform.errors import (
    COMMERCE_FACT_UNSUPPORTED,
    COMMERCE_JOB_CANCELLED,
    COMMERCE_OVERSELL,
    COMMERCE_PRICE_CHANGED,
    COMMERCE_SYNC_LOOP_TERMINATED,
    ProductPlatformError,
)
from commerce.product_platform.models import (
    MATCH_AMBIGUOUS,
    MATCH_MATCHED,
    MATCH_NEW,
    ORDER_FULFILLED,
    ORDER_RETURNED,
    PLATFORM_SCHEMA_VERSION,
    OWNERSHIP_POLICY_VERSION,
    MoneyAmount,
)
from commerce.product_platform.one_c import FakeOneCAdapter
from commerce.product_platform.ownership import default_ownership_policy, detect_sync_conflict
from commerce.product_platform.service import ProductPlatformService
from commerce.product_platform.sync import SyncEventLedger, plan_sync


def _svc() -> ProductPlatformService:
    return ProductPlatformService()


class ContractTests(unittest.TestCase):
    def test_schema_versions(self):
        self.assertEqual(PLATFORM_SCHEMA_VERSION, "1.0.0")
        self.assertTrue(OWNERSHIP_POLICY_VERSION)


class IdentityMatchTests(unittest.TestCase):
    def test_sku_match_and_ambiguous_title(self):
        svc = _svc()
        p1 = svc.create_product_version(tenant_id="tenant-a", title="Widget", sku="W-001")
        svc.create_product_version(tenant_id="tenant-a", title="Same Title", sku="A-1")
        svc.create_product_version(tenant_id="tenant-a", title="Same Title", sku="A-2")
        m = svc.match_product(tenant_id="tenant-a", sku="W-001")
        self.assertEqual(m.state, MATCH_MATCHED)
        self.assertEqual(m.product_id, p1.product_id)
        amb = svc.match_product(tenant_id="tenant-a", title="Same Title")
        self.assertEqual(amb.state, MATCH_AMBIGUOUS)
        self.assertGreaterEqual(len(amb.candidate_refs), 2)


class SourceSnapshotTests(unittest.TestCase):
    def test_snapshot_checksum(self):
        svc = _svc()
        snap = svc.ingest_source_snapshot(
            tenant_id="tenant-a",
            source="supplier",
            external_id="sup-1",
            fields={"sku": "00123", "title": "Item"},
        )
        self.assertEqual(len(snap.checksum), 64)
        self.assertEqual(snap.normalized_fields["sku"], "00123")


class ImportDryRunTests(unittest.TestCase):
    def test_dry_run_no_mutation(self):
        svc = _svc()
        rows = [{"sku": "S1", "title": "One"}, {"sku": "S2", "title": "Two"}]
        preview = svc.import_products(tenant_id="tenant-a", rows=rows, dry_run=True)
        self.assertTrue(preview.dry_run)
        self.assertEqual(preview.created, 2)
        self.assertIsNone(svc.repo.find_by_identifier("tenant-a", "sku", "S1"))


class FactLockTests(unittest.TestCase):
    def test_enrichment_rejects_invented_claims(self):
        svc = _svc()
        p = svc.create_product_version(tenant_id="tenant-a", title="X", sku="X1")
        with self.assertRaises(ProductPlatformError) as ctx:
            svc.enrich_product(
                tenant_id="tenant-a",
                product_id=p.product_id,
                generated_description="Buy now with certified warranty and free delivery tomorrow",
            )
        self.assertEqual(ctx.exception.code, COMMERCE_FACT_UNSUPPORTED)


class CategoryBrandTests(unittest.TestCase):
    def test_register_category_brand(self):
        svc = _svc()
        cat = svc.register_category(tenant_id="tenant-a", name="Phones")
        brand = svc.register_brand(tenant_id="tenant-a", name="Acme", aliases=("ACME",))
        self.assertTrue(cat.category_id)
        self.assertEqual(brand.normalized_name, "acme")


class PriceDecimalFloorTests(unittest.TestCase):
    def test_money_is_decimal(self):
        m = MoneyAmount(Decimal("19.99"), "RUB")
        self.assertIsInstance(m.amount, Decimal)


class StockReservationTests(unittest.TestCase):
    def test_oversale_and_release(self):
        svc = _svc()
        p = svc.create_product_version(tenant_id="tenant-a", title="Stocked", sku="ST1")
        svc.observe_stock(tenant_id="tenant-a", product_id=p.product_id, location_id="main", on_hand=Decimal("1"))
        r1 = svc.reserve_stock(
            tenant_id="tenant-a",
            product_id=p.product_id,
            location_id="main",
            quantity=Decimal("1"),
            idempotency_key="k1",
        )
        with self.assertRaises(ProductPlatformError) as ctx:
            svc.reserve_stock(
                tenant_id="tenant-a",
                product_id=p.product_id,
                location_id="main",
                quantity=Decimal("1"),
                idempotency_key="k2",
            )
        self.assertEqual(ctx.exception.code, COMMERCE_OVERSELL)
        released = svc.release_stock(tenant_id="tenant-a", reservation_id=r1["reservation_id"])
        self.assertEqual(released["status"], "released")
        r2 = svc.reserve_stock(
            tenant_id="tenant-a",
            product_id=p.product_id,
            location_id="main",
            quantity=Decimal("1"),
            idempotency_key="k3",
        )
        self.assertEqual(r2["status"], "reserved")


class CartCheckoutTests(unittest.TestCase):
    def test_reprice_detection(self):
        cart = create_cart(
            tenant_id="tenant-a",
            lines=[{"sku": "S", "product_id": "p1", "quantity": 1, "unit_price": "10.00"}],
        )
        result = revalidate_checkout(
            cart=cart,
            current_prices={"p1": Decimal("12.00")},
            current_availability={"p1": "IN_STOCK"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], COMMERCE_PRICE_CHANGED)


class OrderReturnTransitionTests(unittest.TestCase):
    def test_fulfilled_to_returned(self):
        from commerce.product_platform.models import ORDER_TRANSITIONS

        self.assertIn(ORDER_RETURNED, ORDER_TRANSITIONS[ORDER_FULFILLED])


class OwnershipSyncTests(unittest.TestCase):
    def test_conflict_and_loop_prevention(self):
        policy = default_ownership_policy(tenant_id="tenant-a")
        conflict = detect_sync_conflict(
            tenant_id="tenant-a",
            entity_type="product",
            entity_id="p1",
            field="stock",
            canonical_value="5",
            external_value="7",
            policy=policy,
        )
        self.assertIsNotNone(conflict)
        plan = plan_sync(
            tenant_id="tenant-a",
            integration="bitrix",
            direction="PUSH",
            changes=[{"entity_type": "product", "entity_id": "p1", "field": "stock", "canonical_value": "5", "external_value": "7"}],
            policy=policy,
            dry_run=True,
        )
        self.assertEqual(len(plan.conflicts), 1)
        ledger = SyncEventLedger()
        ledger.record_outbound(causation_id="c-1", plan_id=plan.plan_id)
        ack = ledger.acknowledge_inbound(causation_id="c-1", origin="bitrix")
        self.assertTrue(ack["terminated"])
        self.assertEqual(ack["code"], COMMERCE_SYNC_LOOP_TERMINATED)


class AsproBitrixTests(unittest.TestCase):
    def test_aspro_maps_to_bitrix_payload(self):
        profile = fixture_aspro_premier_profile()
        self.assertEqual(profile.source_of_rules, "configurable")
        payload = map_product_to_bitrix_payload(
            product={"title": "Phone", "sku": "P1", "brand": "Acme", "description": "desc"},
            profile=profile,
        )
        self.assertEqual(payload["NAME"], "Phone")
        self.assertEqual(payload["storefront"], "aspro_premier")
        svc = _svc()
        p = svc.create_product_version(tenant_id="tenant-a", title="Phone", sku="P1", brand="Acme")
        preview = svc.bitrix_sync_preview(tenant_id="tenant-a", product_id=p.product_id)
        self.assertTrue(preview["dry_run"])
        ack = svc.acknowledge_reflected_event(causation_id=preview["causation_id"], origin="bitrix")
        self.assertTrue(ack["terminated"])


class OneCTests(unittest.TestCase):
    def test_one_c_fixture_import(self):
        adapter = FakeOneCAdapter()
        self.assertFalse(adapter.capabilities()["live"])
        svc = _svc()
        out = svc.one_c_seed_and_import(
            tenant_id="tenant-a",
            guid="guid-1",
            sku="1C-SKU",
            title="From 1C",
            price="199.00",
            stock="3",
        )
        self.assertEqual(out["match_state"], MATCH_NEW)
        self.assertTrue(out["fake"])
        push = svc.one_c_push_order(order={"order_id": "o1"}, idempotency_key="idem-1")
        push2 = svc.one_c_push_order(order={"order_id": "o1"}, idempotency_key="idem-1")
        self.assertTrue(push2["idempotent"])
        self.assertEqual(push["external_id"], push2["external_id"])


class HandoffTests(unittest.TestCase):
    def test_content_media_seo_marketplace_views(self):
        svc = _svc()
        p = svc.create_product_version(tenant_id="tenant-a", title="Handoff", sku="H1")
        content = svc.content_handoff(tenant_id="tenant-a", product_id=p.product_id)
        self.assertEqual(content["context"]["delegate_generation_to"], "content_intel")
        media = svc.media_handoff(tenant_id="tenant-a", product_id=p.product_id)
        self.assertEqual(media["delegate_to"], "product_media")
        seo = svc.seo_handoff(tenant_id="tenant-a", product_id=p.product_id)
        self.assertEqual(seo["delegate_to"], "seo_marketing")
        view = svc.marketplace_export_view(tenant_id="tenant-a", product_id=p.product_id)
        self.assertEqual(view.sku, "H1")


class JobCancelTests(unittest.TestCase):
    def test_cancel_job(self):
        svc = _svc()
        p = svc.create_product_version(tenant_id="tenant-a", title="J", sku="J1")
        job = svc.start_cms_bulk_sync(
            tenant_id="tenant-a",
            product_specs=[{"product_id": p.product_id, "version_id": p.version_id}],
            bulk=True,
        )
        cancelled = svc.cancel_job(tenant_id="tenant-a", job_id=job["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["code"], COMMERCE_JOB_CANCELLED)


class TenantTests(unittest.TestCase):
    def test_cross_tenant_product_hidden(self):
        svc = _svc()
        p = svc.create_product_version(tenant_id="tenant-a", title="Priv", sku="PRIV1")
        self.assertIsNone(svc.get_product(tenant_id="tenant-b", product_id=p.product_id))


if __name__ == "__main__":
    unittest.main()
