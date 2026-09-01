"""Real Bitrix / Aspro Premier integration — closure tests."""

from __future__ import annotations

import copy
import os
import re
import shutil
import tempfile
import unittest

from business_assistant.models import STATUS_WAITING_FOR_APPROVAL
from business_assistant.service import BusinessAssistantService
from integrations.activation.errors import (
    IntegrationCrossTenantError,
    IntegrationLiveFallbackForbiddenError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationWriteDeniedError,
)
from integrations.activation.models import ENV_FIXTURE, ENV_LIVE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.catalog import BitrixCatalogStore, GLOBAL_BITRIX_CATALOG
from integrations.bitrix.errors import (
    BitrixAmbiguousTargetError,
    BitrixNotFoundError,
    BitrixWriteVerificationFailedError,
)
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter, BitrixFixtureState
from integrations.bitrix.live_adapter import LiveBitrixAdapter
from integrations.bitrix.mapping import build_preview, selective_export_filter
from integrations.bitrix.webhooks import BitrixWebhookReadiness


def _svc(store: BitrixCatalogStore | None = None) -> IntegrationActivationService:
    svc = IntegrationActivationService()
    if store is not None:
        adapter = BitrixFixtureAdapter(store=store)
        svc._adapters["bitrix"] = adapter
        svc._bitrix_fixture = adapter
    return svc


def _active(svc: IntegrationActivationService, *, tenant: str, provider: str = "bitrix", env: str = ENV_FIXTURE):
    ref = svc.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:{provider}-{tenant}", value=f"tok-{provider}-{tenant}")
    conn = svc.configure_connection(
        tenant_id=tenant, provider_id=provider, credential_ref=ref, environment=env
    )
    svc.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)
    return conn


class BitrixCatalogTests(unittest.TestCase):
    def test_product_lookup_by_article(self):
        store = BitrixCatalogStore()
        adapter = BitrixFixtureAdapter(store=store)
        out = adapter.read(
            capability="cms.bitrix.catalog.read",
            params={"operation": "product_lookup", "article": "SKU-X100"},
            tenant_id="tenant-a",
        )
        self.assertEqual(out["product"]["article"], "SKU-X100")
        self.assertEqual(out["product"]["name"], "Samsung Galaxy S24")
        self.assertFalse(out["live"])

    def test_ambiguous_lookup_fails_closed(self):
        store = BitrixCatalogStore()
        adapter = BitrixFixtureAdapter(store=store)
        adapter.state.force_ambiguous = True
        with self.assertRaises(BitrixAmbiguousTargetError):
            adapter.read(
                capability="cms.bitrix.catalog.read",
                params={"operation": "product_lookup", "name": "Samsung Accessory"},
                tenant_id="tenant-a",
            )

    def test_price_and_stock_read(self):
        adapter = BitrixFixtureAdapter()
        price = adapter.read(
            capability="cms.bitrix.catalog.read",
            params={"operation": "price_read", "article": "SKU-X100-BLK"},
            tenant_id="tenant-a",
        )
        self.assertEqual(price["price"]["amount"], "49990.00")
        stock = adapter.read(
            capability="cms.bitrix.catalog.read",
            params={"operation": "stock_read", "article": "SKU-X100"},
            tenant_id="tenant-a",
        )
        self.assertEqual(stock["total"], 10)


class BitrixWriteGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.store = BitrixCatalogStore()
        self.svc = _svc(self.store)
        _active(self.svc, tenant="tenant-a")

    def test_price_change_preview_approve_verify(self):
        preview = build_preview(
            operation="price_update",
            before={"article": "SKU-X100-BLK", "price": {"amount": "49990.00"}},
            after={"article": "SKU-X100-BLK", "price": {"amount": "49990.00"}},
        )
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="cms.bitrix.catalog.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={
                    "operation": "price_update",
                    "article": "SKU-X100-BLK",
                    "new_price": "49990.00",
                    "preview": preview,
                },
                idempotency_key="price-k1",
                approved_write=False,
            )
        self.assertEqual(self.store.write_count("price-k1"), 0)
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "price_update",
                "article": "SKU-X100-BLK",
                "new_price": "49990.00",
                "preview": preview,
            },
            idempotency_key="price-k1",
            approved_write=True,
        )
        self.assertEqual(w1["result"]["verified"], "VERIFIED")
        self.assertEqual(self.store.write_count("price-k1"), 1)
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "price_update",
                "article": "SKU-X100-BLK",
                "new_price": "49990.00",
                "preview": preview,
            },
            idempotency_key="price-k1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertEqual(self.store.write_count("price-k1"), 1)

    def test_product_create_once_with_mapping(self):
        payload = {
            "operation": "product_create",
            "panda_product_id": "panda-prod-99",
            "product": {"title": "New Phone", "sku": "SKU-NEW-1", "price": "30000", "currency": "RUB"},
            "aspro_premier_enabled": True,
        }
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="create-k1",
            approved_write=True,
        )
        bid = w1["result"]["product"]["external_product_id"]
        self.assertEqual(self.store.get_mapping(tenant_id="tenant-a", panda_product_id="panda-prod-99"), bid)
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="create-k1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertEqual(w1["result"]["write_id"], w2["result"]["write_id"])

    def test_product_update_wrong_target_no_write(self):
        with self.assertRaises(BitrixNotFoundError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="cms.bitrix.catalog.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "product_update", "article": "NO-SUCH-SKU", "changes": {"name": "X"}},
                idempotency_key="upd-bad",
                approved_write=True,
            )

    def test_publish_inactive_product(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "publish", "article": "SKU-X200"},
            idempotency_key="pub-k1",
            approved_write=True,
        )
        self.assertTrue(out["result"]["product"]["active"])
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_stock_write(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "stock_update", "article": "SKU-X100", "quantity": 7},
            idempotency_key="stock-k1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["stock"]["new"], 7)
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_verification_failure_not_success(self):
        self.svc._adapters["bitrix"].state.verification_mismatch = True
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "publish", "article": "SKU-X200"},
            idempotency_key="pub-vfail",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFICATION_FAILED")


class BitrixTenantIsolationTests(unittest.TestCase):
    def test_tenant_b_cannot_use_tenant_a_connection(self):
        svc = _svc()
        conn = _active(svc, tenant="tenant-a")
        with self.assertRaises(IntegrationCrossTenantError):
            svc.get_connection(tenant_id="tenant-b", connection_id=conn.connection_id)

    def test_tenant_catalog_isolated(self):
        store = BitrixCatalogStore()
        adapter = BitrixFixtureAdapter(store=store)
        _ = adapter.write(
            capability="cms.bitrix.catalog.write",
            payload={
                "operation": "product_create",
                "product": {"title": "Tenant B Only", "sku": "SKU-B-ONLY", "price": "100"},
            },
            idempotency_key="b-create",
            tenant_id="tenant-b",
        )
        with self.assertRaises(BitrixNotFoundError):
            adapter.read(
                capability="cms.bitrix.catalog.read",
                params={"operation": "product_lookup", "article": "SKU-B-ONLY"},
                tenant_id="tenant-a",
            )


class BitrixSecretSafetyTests(unittest.TestCase):
    def test_secret_not_in_evidence(self):
        svc = _svc()
        secret = "SUPERSECRETWEBHOOK999"
        ref = svc.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:bitrix-a", value=secret)
        conn = svc.configure_connection(
            tenant_id="tenant-a", provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
        )
        svc.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
        )
        svc.assert_no_secrets_in_evidence(tenant_id="tenant-a")
        status = svc.connection_status_safe(tenant_id="tenant-a", connection_id=conn.connection_id)
        self.assertNotIn(secret, str(status))


class BitrixLiveSafetyTests(unittest.TestCase):
    def test_live_without_config_fails_closed(self):
        svc = _svc()
        ref = svc.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:bitrix-live", value="")
        conn = svc.configure_connection(
            tenant_id="tenant-a",
            provider_id="bitrix",
            credential_ref=ref,
            environment=ENV_LIVE,
        )
        with self.assertRaises(Exception):
            svc.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)

    def test_live_no_fixture_fallback(self):
        svc = _svc()
        _active(svc, tenant="tenant-a", env=ENV_FIXTURE)
        with self.assertRaises(IntegrationLiveFallbackForbiddenError):
            svc.resolve_connection(
                tenant_id="tenant-a",
                capability="cms.bitrix.catalog.read",
                environment=ENV_LIVE,
            )

    def test_live_adapter_blocks_writes_engineering(self):
        os.environ.pop("BITRIX_WEBHOOK_URL", None)
        adapter = LiveBitrixAdapter()
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.write(
                capability="cms.bitrix.catalog.write",
                payload={"operation": "price_update"},
                idempotency_key="x",
            )


class BitrixRateLimitTests(unittest.TestCase):
    def test_rate_limit_normalized(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        svc.adapter_state("bitrix").rate_limited = True
        with self.assertRaises(IntegrationRateLimitedError):
            svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="cms.bitrix.catalog.read",
                environment=ENV_FIXTURE,
                operation_class="READ",
            )


class BitrixExcelSelectiveExportTests(unittest.TestCase):
    def test_only_selected_products_exported(self):
        products = [
            {"sku": "S1", "title": "Phone", "price": "1000"},
            {"sku": "S2", "title": "Case", "price": "100"},
            {"sku": "S3", "title": "Charger", "price": "200"},
        ]
        filtered = selective_export_filter(all_products=products, selected=["S1"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["sku"], "S1")

    def test_selective_export_write_plan(self):
        store = BitrixCatalogStore()
        svc = _svc(store)
        _active(svc, tenant="tenant-a")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "selective_export",
                "products": [
                    {"sku": "S1", "title": "Phone", "price": "1000"},
                    {"sku": "S2", "title": "Case", "price": "100"},
                ],
                "selected": ["S1"],
            },
            idempotency_key="excel-export-1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["exported_count"], 1)
        self.assertEqual(out["result"]["skipped_count"], 1)


class BitrixWebhookReadinessTests(unittest.TestCase):
    def test_verify_normalize_dedupe(self):
        wh = BitrixWebhookReadiness()
        body = b'{"event":"ONCRMPRODUCTUPDATE","data":{"FIELDS":{"ID":"1"}}}'
        sig = __import__("hmac").new(b"secret", body, __import__("hashlib").sha256).hexdigest()
        self.assertTrue(wh.verify_signature(body=body, secret="secret", signature=sig))
        ev1 = wh.normalize(tenant_id="tenant-a", raw={"event": "ONCRMPRODUCTUPDATE", "data": {"FIELDS": {"ID": "1"}}}, verified=True)
        ev2 = wh.normalize(tenant_id="tenant-a", raw={"event": "ONCRMPRODUCTUPDATE", "data": {"FIELDS": {"ID": "1"}}}, verified=True)
        self.assertTrue(ev2.payload_summary.get("duplicate"))
        canonical = wh.to_canonical_event(ev1)
        self.assertEqual(canonical["policy"], "NO_DIRECT_WRITE")


class BitrixRestartRecoveryTests(unittest.TestCase):
    def test_mapping_survives_store_rebind(self):
        store = BitrixCatalogStore()
        adapter = BitrixFixtureAdapter(store=store)
        adapter.write(
            capability="cms.bitrix.catalog.write",
            payload={
                "operation": "product_create",
                "panda_product_id": "panda-42",
                "product": {"title": "Persist", "sku": "SKU-P42", "price": "500"},
            },
            idempotency_key="persist-k1",
            tenant_id="tenant-a",
        )
        bid = store.get_mapping(tenant_id="tenant-a", panda_product_id="panda-42")
        self.assertTrue(bid)
        # Simulate service restart with same store instance
        svc2 = _svc(store)
        _active(svc2, tenant="tenant-a")
        out = svc2.execute_via_gateway(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "product_lookup", "panda_product_id": "panda-42"},
        )
        self.assertEqual(out["result"]["product"]["external_product_id"], bid)


class BitrixBusinessAssistantE2ETests(unittest.TestCase):
    def test_ba_product_read_by_article(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Покажи товар с артикулом SKU-X100 в Bitrix",
            read_only=True,
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertTrue(any(a.get("type") == "bitrix_product" for a in ex.artifacts))

    def test_ba_bitrix_publish_governed(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        ba.seed_supplier_fixture(
            rows=[{"sku": "S1", "brand": "Samsung", "title": "Phone", "price": "2000", "ambiguous": False}],
            costs={"S1": "1000"},
        )
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Опубликуй подготовленные товары Samsung на сайт Bitrix",
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertEqual(ex.status, STATUS_WAITING_FOR_APPROVAL)
        self.assertEqual(len(ba._external_writes), 0)
        ex2 = ba.approve(
            execution_id=ex.execution_id,
            tenant_id="tenant-a",
            actor_id="u",
            approval_id=ex.approval.approval_id,
            plan_fingerprint=ex.plan_fingerprint,
        )
        self.assertEqual(len(ba._external_writes), 1)
        ba.resume(execution_id=ex2.execution_id, tenant_id="tenant-a")
        self.assertEqual(len(ba._external_writes), 1)


class BitrixPaginationTests(unittest.TestCase):
    def test_bounded_pagination(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        pages = svc.paginated_read(
            tenant_id="tenant-a",
            capability="cms.bitrix.catalog.read",
            environment=ENV_FIXTURE,
            max_pages=3,
        )
        self.assertTrue(pages["bounded"])
        self.assertLessEqual(len(pages["pages"]), 3)


if __name__ == "__main__":
    unittest.main()
