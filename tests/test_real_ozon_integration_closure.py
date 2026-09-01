"""Real Ozon integration — closure tests."""

from __future__ import annotations

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
from integrations.ozon.catalog import OzonCatalogStore
from integrations.ozon.config import ozon_engineering_ready, ozon_live_active, ozon_live_verified
from integrations.ozon.errors import (
    OzonAmbiguousTargetError,
    OzonFulfillmentBoundaryError,
    OzonNotFoundError,
    OzonPriceFloorError,
    OzonUncertainWriteOutcomeError,
    OzonUnsupportedCapabilityError,
)
from integrations.ozon.fixture_adapter import OzonFixtureAdapter, OzonFixtureState
from integrations.ozon.live_adapter import LiveOzonAdapter
from integrations.ozon.mapping import build_preview, selective_rows
from integrations.ozon.webhooks import OzonWebhookReadiness


def _svc(store: OzonCatalogStore | None = None) -> IntegrationActivationService:
    svc = IntegrationActivationService()
    if store is not None:
        adapter = OzonFixtureAdapter(store=store)
        svc._adapters["ozon"] = adapter
        svc._ozon_fixture = adapter
    return svc


def _active(svc: IntegrationActivationService, *, tenant: str, env: str = ENV_FIXTURE):
    ref = svc.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:oz-{tenant}", value=f"cid-{tenant}:key-{tenant}")
    conn = svc.configure_connection(tenant_id=tenant, provider_id="ozon", credential_ref=ref, environment=env)
    svc.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)
    return conn


class OzonFixtureConfigTests(unittest.TestCase):
    def test_fixture_connection_works(self):
        svc = _svc()
        conn = _active(svc, tenant="tenant-a")
        self.assertEqual(conn.provider_id, "ozon")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "price_read", "seller_article": "OZ-SKU-100"},
        )
        self.assertEqual(out["result"]["mode"], "FIXTURE")
        self.assertFalse(out["live"])

    def test_engineering_flags(self):
        self.assertTrue(ozon_engineering_ready())
        self.assertFalse(ozon_live_active())
        self.assertFalse(ozon_live_verified())


class OzonIdentityTests(unittest.TestCase):
    def test_product_id_offer_id_not_conflated(self):
        adapter = OzonFixtureAdapter(store=OzonCatalogStore())
        out = adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "card_lookup", "seller_article": "OZ-SKU-100"},
            tenant_id="tenant-a",
        )
        card = out["card"]
        self.assertEqual(card["product_id"], 701001)
        self.assertEqual(card["offer_id"], "OFFER-100")
        self.assertNotEqual(str(card["product_id"]), card["offer_id"])

    def test_lookup_by_distinct_identifiers(self):
        store = OzonCatalogStore()
        adapter = OzonFixtureAdapter(store=store)
        by_offer = adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "card_lookup", "offer_id": "OFFER-100"},
            tenant_id="tenant-a",
        )
        by_barcode = adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "card_lookup", "barcode": "4600000007001"},
            tenant_id="tenant-a",
        )
        self.assertEqual(by_offer["card"]["seller_article"], by_barcode["card"]["seller_article"])


class OzonReadTests(unittest.TestCase):
    def test_price_read_semantics(self):
        adapter = OzonFixtureAdapter()
        out = adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "price_read", "seller_article": "OZ-SKU-100"},
            tenant_id="tenant-a",
        )
        self.assertEqual(out["seller_price"], "1990.00")
        self.assertEqual(out["old_price"], "2490.00")
        self.assertEqual(out["seller_price_control"], "SELLER_CONTROLLED")

    def test_platform_controlled_price(self):
        adapter = OzonFixtureAdapter()
        out = adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "price_read", "seller_article": "OZ-SKU-200"},
            tenant_id="tenant-a",
        )
        self.assertEqual(out["customer_price_control"], "PLATFORM_CONTROLLED")
        self.assertNotEqual(out["seller_price"], out["customer_visible_price"])

    def test_stock_warehouse_distinction(self):
        adapter = OzonFixtureAdapter()
        main = adapter.read(
            capability="marketplace.ozon.stock.read",
            params={"operation": "stock_read", "seller_article": "OZ-SKU-100", "warehouse": "fbs_main"},
            tenant_id="tenant-a",
        )
        east = adapter.read(
            capability="marketplace.ozon.stock.read",
            params={"operation": "stock_read", "seller_article": "OZ-SKU-100", "warehouse": "fbs_east"},
            tenant_id="tenant-a",
        )
        self.assertNotEqual(main["warehouse_id"], east["warehouse_id"])
        self.assertNotEqual(main["available"], east["available"])

    def test_orders_read(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.orders.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "order_read", "page": 1},
        )
        self.assertTrue(out["result"]["items"])
        self.assertEqual(out["result"]["mode"], "FIXTURE")

    def test_promotion_read_only(self):
        adapter = OzonFixtureAdapter()
        out = adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "promotion_read"},
            tenant_id="tenant-a",
        )
        self.assertTrue(out["items"])


class OzonAsyncImportTests(unittest.TestCase):
    def setUp(self):
        self.store = OzonCatalogStore()
        self.adapter = OzonFixtureAdapter(store=self.store, state=OzonFixtureState(import_outcome="PROCESSING"))

    def test_import_submission_not_final_success(self):
        out = self.adapter.write(
            capability="marketplace.ozon.price.write",
            payload={
                "operation": "card_import",
                "product": {"seller_article": "OZ-NEW-1", "offer_id": "OFFER-NEW", "title": "New", "category_id": "phones"},
            },
            idempotency_key="oz-import-1",
            tenant_id="tenant-a",
        )
        self.assertEqual(out["status"], "SUBMITTED")
        self.assertFalse(out["terminal_success"])
        self.assertEqual(out["verified"], "ACCEPTED_PENDING")

    def test_import_status_processing(self):
        out = self.adapter.write(
            capability="marketplace.ozon.price.write",
            payload={"operation": "card_import", "product": {"seller_article": "OZ-NEW-2", "title": "T"}},
            idempotency_key="oz-import-2",
            tenant_id="tenant-a",
        )
        status = self.adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "import_status", "task_id": out["task_id"]},
            tenant_id="tenant-a",
        )
        self.assertEqual(status["status"], "PROCESSING")

    def test_import_rejection(self):
        self.adapter.state.import_outcome = "REJECTED"
        out = self.adapter.write(
            capability="marketplace.ozon.price.write",
            payload={"operation": "card_import", "product": {"seller_article": "OZ-BAD", "title": "Bad"}},
            idempotency_key="oz-import-rej",
            tenant_id="tenant-a",
        )
        status = self.adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "import_status", "task_id": out["task_id"]},
        )
        self.assertEqual(status["status"], "REJECTED")

    def test_import_success_via_status(self):
        self.adapter.state.import_outcome = "SUCCEEDED"
        out = self.adapter.write(
            capability="marketplace.ozon.price.write",
            payload={"operation": "card_import", "product": {"seller_article": "OZ-OK", "title": "Ok"}},
            idempotency_key="oz-import-ok",
            tenant_id="tenant-a",
        )
        status = self.adapter.read(
            capability="marketplace.ozon.price.read",
            params={"operation": "import_status", "task_id": out["task_id"]},
        )
        self.assertEqual(status["status"], "SUCCEEDED")
        self.assertTrue(status["product_id"])


class OzonWriteGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.store = OzonCatalogStore()
        self.svc = _svc(self.store)
        _active(self.svc, tenant="tenant-a")

    def test_zero_write_before_approval(self):
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.ozon.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990"},
                idempotency_key="oz-p0",
                approved_write=False,
            )
        self.assertEqual(self.store.write_count("oz-p0"), 0)

    def test_governed_price_write_once(self):
        preview = build_preview(operation="price_update", before={"seller_price": "1990"}, after={"seller_price": "49990"})
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="oz-p1",
            approved_write=True,
        )
        self.assertEqual(w1["result"]["verified"], "VERIFIED")
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="oz-p1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertEqual(self.store.write_count("oz-p1"), 1)

    def test_price_floor_rejection_zero_write(self):
        with self.assertRaises(OzonPriceFloorError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.ozon.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "500"},
                idempotency_key="oz-floor",
                approved_write=True,
            )
        self.assertEqual(self.store.write_count("oz-floor"), 0)

    def test_platform_promo_no_unauthorized_mutation(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "promotion_analysis", "seller_article": "OZ-SKU-200"},
        )
        self.assertTrue(out["result"].get("provider_controlled"))
        self.assertFalse(out["result"].get("mutate"))

    def test_promotion_write_not_supported(self):
        adapter = OzonFixtureAdapter(store=self.store)
        with self.assertRaises(OzonUnsupportedCapabilityError):
            adapter.write(
                capability="marketplace.ozon.price.write",
                payload={"operation": "promotion_write", "promotion_id": "x"},
                idempotency_key="oz-promo-w",
                tenant_id="tenant-a",
            )

    def test_stock_write_exact_warehouse(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "stock_update", "seller_article": "OZ-SKU-100", "warehouse": "fbs_main", "quantity": 15},
            idempotency_key="oz-stock1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_ambiguous_warehouse_zero_write(self):
        with self.assertRaises(OzonAmbiguousTargetError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.ozon.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "stock_update", "seller_article": "OZ-SKU-100", "warehouse": "", "quantity": 5},
                idempotency_key="oz-stock-bad",
                approved_write=True,
            )

    def test_fbo_fbs_boundary(self):
        with self.assertRaises(OzonFulfillmentBoundaryError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.ozon.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "stock_update", "seller_article": "OZ-SKU-100", "warehouse": "fbo_main", "quantity": 5},
                idempotency_key="oz-fbo-bad",
                approved_write=True,
            )

    def test_uncertain_write(self):
        self.svc._adapters["ozon"].state.uncertain_write = True
        with self.assertRaises(OzonUncertainWriteOutcomeError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.ozon.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990"},
                idempotency_key="oz-u1",
                approved_write=True,
            )

    def test_reconcile_uncertain_write(self):
        self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990"},
            idempotency_key="oz-rec",
            approved_write=True,
        )
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "reconcile_price", "seller_article": "OZ-SKU-100", "expected_price": "49990"},
            idempotency_key="oz-rec2",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_verification_mismatch(self):
        self.svc._adapters["ozon"].state.verification_mismatch = True
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "stock_update", "seller_article": "OZ-SKU-100", "warehouse": "fbs_main", "quantity": 20},
            idempotency_key="oz-mismatch",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFICATION_FAILED")

    def test_idempotent_card_import(self):
        payload = {"operation": "card_import", "product": {"seller_article": "OZ-IDEM", "title": "Idem"}}
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="oz-card-idem",
            approved_write=True,
        )
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="oz-card-idem",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertEqual(w1["result"]["task_id"], w2["result"]["task_id"])


class OzonSecurityTests(unittest.TestCase):
    def test_tenant_isolation_connection(self):
        svc = _svc()
        conn = _active(svc, tenant="tenant-a")
        with self.assertRaises(IntegrationCrossTenantError):
            svc.get_connection(tenant_id="tenant-b", connection_id=conn.connection_id)

    def test_tenant_product_isolation(self):
        store = OzonCatalogStore()
        a = store.read_price(tenant_id="tenant-a", seller_article="OZ-SKU-100")
        b = store.read_price(tenant_id="tenant-b", seller_article="OZ-B-SKU-100")
        self.assertNotEqual(a["product_id"], b["product_id"])

    def test_tenant_warehouse_isolation(self):
        store = OzonCatalogStore()
        wh_a = store.warehouse_id("tenant-a", "fbs_main")
        wh_b = store.warehouse_id("tenant-b", "fbs_main")
        self.assertNotEqual(wh_a, wh_b)

    def test_secret_not_in_evidence(self):
        svc = _svc()
        secret = "OZON_API_KEY_SUPERSECRET999"
        ref = svc.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:oz-a", value=f"cid:{secret}")
        conn = svc.configure_connection(tenant_id="tenant-a", provider_id="ozon", credential_ref=ref, environment=ENV_FIXTURE)
        svc.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "price_read", "seller_article": "OZ-SKU-100"},
        )
        svc.assert_no_secrets_in_evidence(tenant_id="tenant-a")


class OzonLiveSafetyTests(unittest.TestCase):
    def test_live_no_fixture_fallback(self):
        svc = _svc()
        _active(svc, tenant="tenant-a", env=ENV_FIXTURE)
        with self.assertRaises(IntegrationLiveFallbackForbiddenError):
            svc.resolve_connection(
                tenant_id="tenant-a",
                capability="marketplace.ozon.price.read",
                environment=ENV_LIVE,
            )

    def test_live_without_config_fails_closed(self):
        adapter = LiveOzonAdapter()
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.read(
                capability="marketplace.ozon.price.read",
                params={"operation": "order_read"},
                credential_ref="secret:x",
            )

    def test_live_write_blocked(self):
        adapter = LiveOzonAdapter()
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.write(
                capability="marketplace.ozon.price.write",
                payload={"operation": "price_update"},
                idempotency_key="x",
            )

    def test_live_verify_not_configured(self):
        adapter = LiveOzonAdapter()
        out = adapter.verify(credential_ref="secret:x")
        self.assertFalse(out.get("ok"))


class OzonBatchPaginationTests(unittest.TestCase):
    def test_selective_export_partial_failure(self):
        svc = _svc(OzonCatalogStore())
        _active(svc, tenant="tenant-a")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "selective_export",
                "rows": [
                    {"sku": "OZ-SKU-100", "price": "49990"},
                    {"sku": "OZ-SKU-200", "price": "100"},
                ],
                "selected": ["OZ-SKU-100", "OZ-SKU-200"],
            },
            idempotency_key="oz-batch1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["exported_count"], 1)
        self.assertEqual(out["result"]["failed_count"], 1)

    def test_selective_rows_only_selected(self):
        rows = [{"sku": "A"}, {"sku": "B"}, {"sku": "C"}]
        self.assertEqual(len(selective_rows(all_rows=rows, selected=["A", "C"])), 2)

    def test_pagination_terminates(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        out = svc.paginated_read(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.read",
            environment=ENV_FIXTURE,
            max_pages=10,
        )
        self.assertTrue(out["bounded"])
        self.assertLessEqual(len(out["pages"]), 10)

    def test_rate_limit_normalized(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        svc.adapter_state("ozon").rate_limited = True
        with self.assertRaises(IntegrationRateLimitedError):
            svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.ozon.price.read",
                environment=ENV_FIXTURE,
                operation_class="READ",
            )


class OzonCapabilityTests(unittest.TestCase):
    def test_read_capability_no_write(self):
        svc = _svc()
        conn = _active(svc, tenant="tenant-a")
        conn_ro = svc.configure_connection(
            tenant_id="tenant-a",
            provider_id="ozon",
            credential_ref=conn.credential_ref,
            environment=ENV_FIXTURE,
            write_capabilities=(),
        )
        svc.activate_connection(tenant_id="tenant-a", connection_id=conn_ro.connection_id)
        with self.assertRaises(IntegrationWriteDeniedError):
            svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.ozon.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990"},
                idempotency_key="oz-cap",
                approved_write=True,
                connection_id=conn_ro.connection_id,
            )


class OzonObservabilityTests(unittest.TestCase):
    def test_usage_and_events(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "price_read", "seller_article": "OZ-SKU-100"},
        )
        usage = svc.usage_events(tenant_id="tenant-a")
        self.assertTrue(any(u["provider"] == "ozon" for u in usage))
        events = svc.list_evidence(tenant_id="tenant-a")
        self.assertTrue(any(e.provider_id == "ozon" for e in events))


class OzonRestartRecoveryTests(unittest.TestCase):
    def test_import_task_survives_store_rebind(self):
        store = OzonCatalogStore()
        adapter = OzonFixtureAdapter(store=store, state=OzonFixtureState(import_outcome="PROCESSING"))
        out = adapter.write(
            capability="marketplace.ozon.price.write",
            payload={"operation": "card_import", "product": {"seller_article": "OZ-PERSIST", "title": "Persist"}},
            idempotency_key="oz-persist",
            tenant_id="tenant-a",
        )
        task_id = out["task_id"]
        svc2 = _svc(store)
        _active(svc2, tenant="tenant-a")
        status = svc2.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.ozon.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "import_status", "task_id": task_id},
        )
        self.assertEqual(status["result"]["status"], "PROCESSING")


class OzonNoBypassTests(unittest.TestCase):
    def test_separate_from_bitrix_onec_wb(self):
        svc = _svc()
        oz = type(svc._adapter_for_provider("ozon"))
        self.assertNotEqual(oz, type(svc._adapter_for_provider("bitrix")))
        self.assertNotEqual(oz, type(svc._adapter_for_provider("onec")))
        self.assertNotEqual(oz, type(svc._adapter_for_provider("wildberries")))


class OzonWebhookTests(unittest.TestCase):
    def test_webhook_dedupe(self):
        wh = OzonWebhookReadiness()
        raw = {"message_type": "TYPE_NEW_POSTING", "posting_number": "123"}
        e1 = wh.normalize(tenant_id="tenant-a", raw=raw, verified=True)
        e2 = wh.normalize(tenant_id="tenant-a", raw=raw, verified=True)
        self.assertFalse(e1.payload_summary["duplicate"])
        self.assertTrue(e2.payload_summary["duplicate"])


class OzonBAE2ETests(unittest.TestCase):
    def test_ba_price_stock_read(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Покажи цену и остаток товара OZ-SKU-100 на Ozon",
            read_only=True,
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertTrue(any(a.get("type") == "ozon_price_stock" for a in ex.artifacts))

    def test_ba_price_governed_flow(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Поставь цену товара OZ-SKU-100 на Ozon 49 990 ₽",
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

    def test_ba_platform_discount_alert_only(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Проверь скидку Ozon на товар OZ-SKU-200",
            read_only=True,
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertEqual(len(ba._external_writes), 0)
        self.assertTrue(any(a.get("type") == "ozon_promotion_alert" for a in ex.artifacts))


if __name__ == "__main__":
    unittest.main()
