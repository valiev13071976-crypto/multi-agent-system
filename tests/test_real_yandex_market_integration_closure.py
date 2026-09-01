"""Real Yandex Market integration — closure tests."""

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
from integrations.yandex_market.catalog import YandexMarketCatalogStore
from integrations.yandex_market.config import (
    yandex_market_engineering_ready,
    yandex_market_live_active,
    yandex_market_live_verified,
)
from integrations.yandex_market.errors import (
    YandexMarketAmbiguousTargetError,
    YandexMarketFulfillmentBoundaryError,
    YandexMarketNotFoundError,
    YandexMarketPriceFloorError,
    YandexMarketScopeError,
    YandexMarketUncertainWriteOutcomeError,
    YandexMarketUnsupportedCapabilityError,
)
from integrations.yandex_market.fixture_adapter import YandexMarketFixtureAdapter, YandexMarketFixtureState
from integrations.yandex_market.live_adapter import LiveYandexMarketAdapter
from integrations.yandex_market.mapping import build_preview, selective_rows
from integrations.yandex_market.webhooks import YandexMarketWebhookReadiness


def _svc(store: YandexMarketCatalogStore | None = None) -> IntegrationActivationService:
    svc = IntegrationActivationService()
    if store is not None:
        adapter = YandexMarketFixtureAdapter(store=store)
        svc._adapters["yandex_market"] = adapter
        svc._ym_fixture = adapter
    return svc


def _active(svc: IntegrationActivationService, *, tenant: str, env: str = ENV_FIXTURE):
    ref = svc.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:ym-{tenant}", value=f"oauth-{tenant}")
    conn = svc.configure_connection(tenant_id=tenant, provider_id="yandex_market", credential_ref=ref, environment=env)
    svc.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)
    return conn


class YandexFixtureConfigTests(unittest.TestCase):
    def test_fixture_connection_works(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "price_read", "shop_sku": "YM-SKU-100"},
        )
        self.assertEqual(out["result"]["mode"], "FIXTURE")
        self.assertFalse(out["live"])

    def test_engineering_flags(self):
        self.assertTrue(yandex_market_engineering_ready())
        self.assertFalse(yandex_market_live_active())
        self.assertFalse(yandex_market_live_verified())


class YandexIdentityTests(unittest.TestCase):
    def test_business_campaign_warehouse_distinct(self):
        store = YandexMarketCatalogStore()
        scope = store.business_scope("tenant-a")
        self.assertEqual(scope["business_id"], 100001)
        self.assertEqual(scope["default_campaign"], "camp-a-001")
        wh = store.warehouse_id("tenant-a", "dbs_main")
        self.assertNotEqual(str(scope["business_id"]), wh)

    def test_offer_shop_market_sku_not_conflated(self):
        adapter = YandexMarketFixtureAdapter(store=YandexMarketCatalogStore())
        out = adapter.read(
            capability="marketplace.yandex.price.read",
            params={"operation": "offer_lookup", "shop_sku": "YM-SKU-100"},
            tenant_id="tenant-a",
        )
        offer = out["offer"]
        self.assertEqual(offer["offer_id"], "OFFER-YM-100")
        self.assertEqual(offer["shop_sku"], "YM-SKU-100")
        self.assertEqual(offer["market_sku"], "MKT-801001")
        self.assertNotEqual(offer["offer_id"], offer["market_sku"])


class YandexReadTests(unittest.TestCase):
    def test_price_read_semantics(self):
        adapter = YandexMarketFixtureAdapter()
        out = adapter.read(
            capability="marketplace.yandex.price.read",
            params={"operation": "price_read", "shop_sku": "YM-SKU-100"},
            tenant_id="tenant-a",
        )
        self.assertEqual(out["seller_price"], "1890.00")
        self.assertEqual(out["seller_price_control"], "SELLER_CONTROLLED")

    def test_platform_controlled_price(self):
        adapter = YandexMarketFixtureAdapter()
        out = adapter.read(
            capability="marketplace.yandex.price.read",
            params={"operation": "price_read", "shop_sku": "YM-SKU-200"},
            tenant_id="tenant-a",
        )
        self.assertEqual(out["customer_price_control"], "PLATFORM_CONTROLLED")

    def test_stock_warehouse_distinction(self):
        adapter = YandexMarketFixtureAdapter()
        main = adapter.read(
            capability="marketplace.yandex.stock.read",
            params={"operation": "stock_read", "shop_sku": "YM-SKU-100", "warehouse": "dbs_main"},
            tenant_id="tenant-a",
        )
        east = adapter.read(
            capability="marketplace.yandex.stock.read",
            params={"operation": "stock_read", "shop_sku": "YM-SKU-100", "warehouse": "dbs_east"},
            tenant_id="tenant-a",
        )
        self.assertNotEqual(main["warehouse_id"], east["warehouse_id"])

    def test_orders_read(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.orders.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "order_read", "page": 1},
        )
        self.assertTrue(out["result"]["items"])


class YandexAsyncSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.store = YandexMarketCatalogStore()
        self.adapter = YandexMarketFixtureAdapter(store=self.store, state=YandexMarketFixtureState(submission_outcome="PROCESSING"))

    def test_submission_not_final_success(self):
        out = self.adapter.write(
            capability="marketplace.yandex.price.write",
            payload={"operation": "offer_submission", "product": {"shop_sku": "YM-NEW-1", "title": "New"}},
            idempotency_key="ym-sub-1",
            tenant_id="tenant-a",
        )
        self.assertEqual(out["status"], "SUBMITTED")
        self.assertFalse(out["terminal_success"])
        self.assertEqual(out["verified"], "ACCEPTED_PENDING")

    def test_submission_processing(self):
        out = self.adapter.write(
            capability="marketplace.yandex.price.write",
            payload={"operation": "offer_submission", "product": {"shop_sku": "YM-NEW-2", "title": "T"}},
            idempotency_key="ym-sub-2",
            tenant_id="tenant-a",
        )
        status = self.adapter.read(
            capability="marketplace.yandex.price.read",
            params={"operation": "submission_status", "task_id": out["task_id"]},
        )
        self.assertEqual(status["status"], "PROCESSING")

    def test_submission_rejection(self):
        self.adapter.state.submission_outcome = "REJECTED"
        out = self.adapter.write(
            capability="marketplace.yandex.price.write",
            payload={"operation": "offer_submission", "product": {"shop_sku": "YM-BAD", "title": "Bad"}},
            idempotency_key="ym-sub-rej",
            tenant_id="tenant-a",
        )
        status = self.adapter.read(
            capability="marketplace.yandex.price.read",
            params={"operation": "submission_status", "task_id": out["task_id"]},
        )
        self.assertEqual(status["status"], "REJECTED")

    def test_submission_published_via_status(self):
        self.adapter.state.submission_outcome = "PUBLISHED"
        out = self.adapter.write(
            capability="marketplace.yandex.price.write",
            payload={"operation": "offer_submission", "product": {"shop_sku": "YM-OK", "title": "Ok"}},
            idempotency_key="ym-sub-ok",
            tenant_id="tenant-a",
        )
        status = self.adapter.read(
            capability="marketplace.yandex.price.read",
            params={"operation": "submission_status", "task_id": out["task_id"]},
        )
        self.assertEqual(status["status"], "PUBLISHED")
        self.assertTrue(status["market_sku"])


class YandexWriteGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.store = YandexMarketCatalogStore()
        self.svc = _svc(self.store)
        _active(self.svc, tenant="tenant-a")

    def test_zero_write_before_approval(self):
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.yandex.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "shop_sku": "YM-SKU-100", "new_price": "49990"},
                idempotency_key="ym-p0",
                approved_write=False,
            )
        self.assertEqual(self.store.write_count("ym-p0"), 0)

    def test_governed_price_write_once(self):
        preview = build_preview(operation="price_update", before={"seller_price": "1890"}, after={"seller_price": "49990"})
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "shop_sku": "YM-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="ym-p1",
            approved_write=True,
        )
        self.assertEqual(w1["result"]["verified"], "VERIFIED")
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "shop_sku": "YM-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="ym-p1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertEqual(self.store.write_count("ym-p1"), 1)

    def test_price_floor_rejection_zero_write(self):
        with self.assertRaises(YandexMarketPriceFloorError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.yandex.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "shop_sku": "YM-SKU-100", "new_price": "500"},
                idempotency_key="ym-floor",
                approved_write=True,
            )
        self.assertEqual(self.store.write_count("ym-floor"), 0)

    def test_platform_promo_no_mutation(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "promotion_analysis", "shop_sku": "YM-SKU-200"},
        )
        self.assertTrue(out["result"].get("provider_controlled"))
        self.assertFalse(out["result"].get("mutate"))

    def test_promotion_write_not_supported(self):
        adapter = YandexMarketFixtureAdapter(store=self.store)
        with self.assertRaises(YandexMarketUnsupportedCapabilityError):
            adapter.write(
                capability="marketplace.yandex.price.write",
                payload={"operation": "promotion_write"},
                idempotency_key="ym-promo",
                tenant_id="tenant-a",
            )

    def test_stock_write_exact_warehouse(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "stock_update", "shop_sku": "YM-SKU-100", "warehouse": "dbs_main", "quantity": 12},
            idempotency_key="ym-stock1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_ambiguous_warehouse(self):
        with self.assertRaises(YandexMarketAmbiguousTargetError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.yandex.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "stock_update", "shop_sku": "YM-SKU-100", "warehouse": "", "quantity": 5},
                idempotency_key="ym-stock-bad",
                approved_write=True,
            )

    def test_fby_dbs_boundary(self):
        with self.assertRaises(YandexMarketFulfillmentBoundaryError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.yandex.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "stock_update", "shop_sku": "YM-SKU-100", "warehouse": "fby_main", "quantity": 5},
                idempotency_key="ym-fby-bad",
                approved_write=True,
            )

    def test_uncertain_write(self):
        self.svc._adapters["yandex_market"].state.uncertain_write = True
        with self.assertRaises(YandexMarketUncertainWriteOutcomeError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.yandex.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "shop_sku": "YM-SKU-100", "new_price": "49990"},
                idempotency_key="ym-u1",
                approved_write=True,
            )

    def test_reconcile_uncertain_write(self):
        self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "shop_sku": "YM-SKU-100", "new_price": "49990"},
            idempotency_key="ym-rec",
            approved_write=True,
        )
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "reconcile_price", "shop_sku": "YM-SKU-100", "expected_price": "49990"},
            idempotency_key="ym-rec2",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_idempotent_submission(self):
        payload = {"operation": "offer_submission", "product": {"shop_sku": "YM-IDEM", "title": "Idem"}}
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="ym-idem",
            approved_write=True,
        )
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="ym-idem",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])


class YandexSecurityTests(unittest.TestCase):
    def test_tenant_isolation(self):
        svc = _svc()
        conn = _active(svc, tenant="tenant-a")
        with self.assertRaises(IntegrationCrossTenantError):
            svc.get_connection(tenant_id="tenant-b", connection_id=conn.connection_id)

    def test_tenant_business_isolation(self):
        store = YandexMarketCatalogStore()
        a = store.business_scope("tenant-a")
        b = store.business_scope("tenant-b")
        self.assertNotEqual(a["business_id"], b["business_id"])
        self.assertNotEqual(a["default_campaign"], b["default_campaign"])

    def test_tenant_offer_isolation(self):
        store = YandexMarketCatalogStore()
        a = store.read_price(tenant_id="tenant-a", shop_sku="YM-SKU-100")
        b = store.read_price(tenant_id="tenant-b", shop_sku="YM-B-SKU-100")
        self.assertNotEqual(a["business_id"], b["business_id"])

    def test_secret_not_in_evidence(self):
        svc = _svc()
        secret = "YANDEX_OAUTH_SUPERSECRET999"
        ref = svc.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:ym-a", value=secret)
        conn = svc.configure_connection(tenant_id="tenant-a", provider_id="yandex_market", credential_ref=ref, environment=ENV_FIXTURE)
        svc.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "price_read", "shop_sku": "YM-SKU-100"},
        )
        svc.assert_no_secrets_in_evidence(tenant_id="tenant-a")

    def test_campaign_scope_mismatch(self):
        store = YandexMarketCatalogStore()
        adapter = YandexMarketFixtureAdapter(store=store)
        with self.assertRaises(YandexMarketScopeError):
            adapter.read(
                capability="marketplace.yandex.price.read",
                params={"operation": "offer_lookup", "shop_sku": "YM-SKU-100", "campaign_id": "camp-b-001"},
                tenant_id="tenant-a",
            )


class YandexLiveSafetyTests(unittest.TestCase):
    def test_live_no_fixture_fallback(self):
        svc = _svc()
        _active(svc, tenant="tenant-a", env=ENV_FIXTURE)
        with self.assertRaises(IntegrationLiveFallbackForbiddenError):
            svc.resolve_connection(
                tenant_id="tenant-a",
                capability="marketplace.yandex.price.read",
                environment=ENV_LIVE,
            )

    def test_live_without_config(self):
        adapter = LiveYandexMarketAdapter()
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.read(capability="marketplace.yandex.price.read", params={"operation": "order_read"}, credential_ref="secret:x")

    def test_live_write_blocked(self):
        adapter = LiveYandexMarketAdapter()
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.write(capability="marketplace.yandex.price.write", payload={"operation": "price_update"}, idempotency_key="x")


class YandexBatchPaginationTests(unittest.TestCase):
    def test_selective_export_partial_failure(self):
        svc = _svc(YandexMarketCatalogStore())
        _active(svc, tenant="tenant-a")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "selective_export",
                "rows": [{"shop_sku": "YM-SKU-100", "price": "49990"}, {"shop_sku": "YM-SKU-200", "price": "100"}],
                "selected": ["YM-SKU-100", "YM-SKU-200"],
            },
            idempotency_key="ym-batch1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["exported_count"], 1)
        self.assertEqual(out["result"]["failed_count"], 1)

    def test_selective_rows(self):
        rows = [{"shop_sku": "A"}, {"shop_sku": "B"}]
        self.assertEqual(len(selective_rows(all_rows=rows, selected=["A"])), 1)

    def test_pagination_terminates(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        out = svc.paginated_read(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.read",
            environment=ENV_FIXTURE,
            max_pages=10,
        )
        self.assertTrue(out["bounded"])

    def test_rate_limit(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        svc.adapter_state("yandex_market").rate_limited = True
        with self.assertRaises(IntegrationRateLimitedError):
            svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.yandex.price.read",
                environment=ENV_FIXTURE,
                operation_class="READ",
            )


class YandexCapabilityTests(unittest.TestCase):
    def test_read_no_write(self):
        svc = _svc()
        conn = _active(svc, tenant="tenant-a")
        conn_ro = svc.configure_connection(
            tenant_id="tenant-a",
            provider_id="yandex_market",
            credential_ref=conn.credential_ref,
            environment=ENV_FIXTURE,
            write_capabilities=(),
        )
        svc.activate_connection(tenant_id="tenant-a", connection_id=conn_ro.connection_id)
        with self.assertRaises(IntegrationWriteDeniedError):
            svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.yandex.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "shop_sku": "YM-SKU-100", "new_price": "49990"},
                idempotency_key="ym-cap",
                approved_write=True,
                connection_id=conn_ro.connection_id,
            )


class YandexObservabilityTests(unittest.TestCase):
    def test_usage_and_evidence(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "price_read", "shop_sku": "YM-SKU-100"},
        )
        usage = svc.usage_events(tenant_id="tenant-a")
        self.assertTrue(any(u["provider"] == "yandex_market" for u in usage))
        events = svc.list_evidence(tenant_id="tenant-a")
        self.assertTrue(any(e.provider_id == "yandex_market" for e in events))


class YandexRestartRecoveryTests(unittest.TestCase):
    def test_submission_survives_store_rebind(self):
        store = YandexMarketCatalogStore()
        adapter = YandexMarketFixtureAdapter(store=store, state=YandexMarketFixtureState(submission_outcome="PROCESSING"))
        out = adapter.write(
            capability="marketplace.yandex.price.write",
            payload={"operation": "offer_submission", "product": {"shop_sku": "YM-PERSIST", "title": "Persist"}},
            idempotency_key="ym-persist",
            tenant_id="tenant-a",
        )
        svc2 = _svc(store)
        _active(svc2, tenant="tenant-a")
        status = svc2.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.yandex.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "submission_status", "task_id": out["task_id"]},
        )
        self.assertEqual(status["result"]["status"], "PROCESSING")


class YandexNoBypassTests(unittest.TestCase):
    def test_separate_from_siblings(self):
        svc = _svc()
        ym = type(svc._adapter_for_provider("yandex_market"))
        self.assertNotEqual(ym, type(svc._adapter_for_provider("ozon")))
        self.assertNotEqual(ym, type(svc._adapter_for_provider("wildberries")))
        self.assertNotEqual(ym, type(svc._adapter_for_provider("onec")))


class YandexWebhookTests(unittest.TestCase):
    def test_dedupe(self):
        wh = YandexMarketWebhookReadiness()
        raw = {"type": "ORDER_STATUS_CHANGED", "orderId": "123"}
        e1 = wh.normalize(tenant_id="tenant-a", raw=raw, verified=True)
        e2 = wh.normalize(tenant_id="tenant-a", raw=raw, verified=True)
        self.assertFalse(e1.payload_summary["duplicate"])
        self.assertTrue(e2.payload_summary["duplicate"])


class YandexBAE2ETests(unittest.TestCase):
    def test_ba_price_stock_read(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Покажи цену и остаток товара YM-SKU-100 на Yandex Market",
            read_only=True,
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertTrue(any(a.get("type") == "yandex_price_stock" for a in ex.artifacts))

    def test_ba_price_governed_flow(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Поставь цену товара YM-SKU-100 на Yandex 49 990 ₽",
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertEqual(ex.status, STATUS_WAITING_FOR_APPROVAL)
        self.assertEqual(len(ba._external_writes), 0)
        ba.approve(
            execution_id=ex.execution_id,
            tenant_id="tenant-a",
            actor_id="u",
            approval_id=ex.approval.approval_id,
            plan_fingerprint=ex.plan_fingerprint,
        )
        self.assertEqual(len(ba._external_writes), 1)

    def test_ba_platform_discount_alert(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Проверь скидку Yandex на товар YM-SKU-200",
            read_only=True,
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertEqual(len(ba._external_writes), 0)
        self.assertTrue(any(a.get("type") == "yandex_promotion_alert" for a in ex.artifacts))


if __name__ == "__main__":
    unittest.main()
