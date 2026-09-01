"""Real Wildberries integration — closure tests."""

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
from integrations.wildberries.catalog import WildberriesCatalogStore
from integrations.wildberries.errors import (
    WildberriesAmbiguousTargetError,
    WildberriesNotFoundError,
    WildberriesPriceFloorError,
    WildberriesUncertainWriteOutcomeError,
)
from integrations.wildberries.fixture_adapter import WildberriesFixtureAdapter, WildberriesFixtureState
from integrations.wildberries.live_adapter import LiveWildberriesAdapter
from integrations.wildberries.mapping import build_preview, selective_rows
from integrations.wildberries.webhooks import WildberriesWebhookReadiness


def _svc(store: WildberriesCatalogStore | None = None) -> IntegrationActivationService:
    svc = IntegrationActivationService()
    if store is not None:
        adapter = WildberriesFixtureAdapter(store=store)
        svc._adapters["wildberries"] = adapter
        svc._wb_fixture = adapter
    return svc


def _active(svc: IntegrationActivationService, *, tenant: str, env: str = ENV_FIXTURE):
    ref = svc.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:wb-{tenant}", value=f"tok-wb-{tenant}")
    conn = svc.configure_connection(tenant_id=tenant, provider_id="wildberries", credential_ref=ref, environment=env)
    svc.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)
    return conn


class WildberriesReadTests(unittest.TestCase):
    def test_card_lookup_nm_id(self):
        adapter = WildberriesFixtureAdapter(store=WildberriesCatalogStore())
        out = adapter.read(
            capability="marketplace.wb.price.read",
            params={"operation": "card_lookup", "seller_article": "WB-SKU-100"},
            tenant_id="tenant-a",
        )
        self.assertEqual(out["card"]["seller_article"], "WB-SKU-100")
        self.assertEqual(out["card"]["nm_id"], 1001001)

    def test_price_read_with_discount_semantics(self):
        adapter = WildberriesFixtureAdapter()
        out = adapter.read(
            capability="marketplace.wb.price.read",
            params={"operation": "price_read", "seller_article": "WB-SKU-100-BLK"},
            tenant_id="tenant-a",
        )
        self.assertEqual(out["base_price"], "2190.00")
        self.assertEqual(out["seller_discount_pct"], "5")

    def test_stock_warehouse_distinction(self):
        adapter = WildberriesFixtureAdapter()
        main = adapter.read(
            capability="marketplace.wb.stock.read",
            params={"operation": "stock_read", "seller_article": "WB-SKU-100", "warehouse": "main"},
            tenant_id="tenant-a",
        )
        east = adapter.read(
            capability="marketplace.wb.stock.read",
            params={"operation": "stock_read", "seller_article": "WB-SKU-100", "warehouse": "east"},
            tenant_id="tenant-a",
        )
        self.assertNotEqual(main["available"], east["available"])


class WildberriesWriteGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.store = WildberriesCatalogStore()
        self.svc = _svc(self.store)
        _active(self.svc, tenant="tenant-a")

    def test_zero_write_before_approval(self):
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.wb.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "seller_article": "WB-SKU-100", "new_price": "49990"},
                idempotency_key="wb-p0",
                approved_write=False,
            )
        self.assertEqual(self.store.write_count("wb-p0"), 0)

    def test_governed_price_write_once(self):
        preview = build_preview(operation="price_update", before={"base_price": "1990"}, after={"base_price": "49990"})
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.wb.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "seller_article": "WB-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="wb-p1",
            approved_write=True,
        )
        self.assertEqual(w1["result"]["verified"], "VERIFIED")
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.wb.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "seller_article": "WB-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="wb-p1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertEqual(self.store.write_count("wb-p1"), 1)

    def test_price_floor_rejection_zero_write(self):
        with self.assertRaises(WildberriesPriceFloorError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.wb.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "seller_article": "WB-SKU-100", "new_price": "500"},
                idempotency_key="wb-floor",
                approved_write=True,
            )
        self.assertEqual(self.store.write_count("wb-floor"), 0)

    def test_platform_promo_no_unauthorized_mutation(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.wb.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
            payload={"operation": "promotion_analysis", "seller_article": "WB-SKU-200"},
        )
        self.assertTrue(out["result"].get("provider_controlled"))
        self.assertFalse(out["result"].get("mutate"))

    def test_card_create_once(self):
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.wb.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "card_create",
                "product": {"seller_article": "WB-NEW-1", "title": "New Item", "category_id": "phones", "purchase_cost": "500"},
            },
            idempotency_key="wb-card1",
            approved_write=True,
        )
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.wb.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "card_create",
                "product": {"seller_article": "WB-NEW-1", "title": "New Item", "category_id": "phones", "purchase_cost": "500"},
            },
            idempotency_key="wb-card1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])

    def test_stock_write_exact_warehouse(self):
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.wb.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "stock_update", "seller_article": "WB-SKU-100", "warehouse": "main", "quantity": 15},
            idempotency_key="wb-stock1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFIED")

    def test_ambiguous_warehouse_zero_write(self):
        with self.assertRaises(WildberriesAmbiguousTargetError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.wb.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "stock_update", "seller_article": "WB-SKU-100", "warehouse": "", "quantity": 5},
                idempotency_key="wb-stock-bad",
                approved_write=True,
            )

    def test_uncertain_write(self):
        self.svc._adapters["wildberries"].state.uncertain_write = True
        with self.assertRaises(WildberriesUncertainWriteOutcomeError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.wb.price.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "seller_article": "WB-SKU-100", "new_price": "49990"},
                idempotency_key="wb-u1",
                approved_write=True,
            )


class WildberriesSecurityTests(unittest.TestCase):
    def test_tenant_isolation(self):
        svc = _svc()
        conn = _active(svc, tenant="tenant-a")
        with self.assertRaises(IntegrationCrossTenantError):
            svc.get_connection(tenant_id="tenant-b", connection_id=conn.connection_id)

    def test_secret_not_in_evidence(self):
        svc = _svc()
        secret = "WB_API_TOKEN_SUPERSECRET999"
        ref = svc.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:wb-a", value=secret)
        conn = svc.configure_connection(tenant_id="tenant-a", provider_id="wildberries", credential_ref=ref, environment=ENV_FIXTURE)
        svc.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.wb.price.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
        )
        svc.assert_no_secrets_in_evidence(tenant_id="tenant-a")


class WildberriesLiveSafetyTests(unittest.TestCase):
    def test_live_no_fixture_fallback(self):
        svc = _svc()
        _active(svc, tenant="tenant-a", env=ENV_FIXTURE)
        with self.assertRaises(IntegrationLiveFallbackForbiddenError):
            svc.resolve_connection(
                tenant_id="tenant-a",
                capability="marketplace.wb.price.read",
                environment=ENV_LIVE,
            )

    def test_live_write_blocked(self):
        adapter = LiveWildberriesAdapter()
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.write(
                capability="marketplace.wb.price.write",
                payload={"operation": "price_update"},
                idempotency_key="x",
            )


class WildberriesBatchBoundaryTests(unittest.TestCase):
    def test_selective_export_partial_failure(self):
        svc = _svc(WildberriesCatalogStore())
        _active(svc, tenant="tenant-a")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="marketplace.wb.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "selective_export",
                "rows": [
                    {"sku": "WB-SKU-100", "price": "49990"},
                    {"sku": "WB-SKU-200", "price": "100"},
                ],
                "selected": ["WB-SKU-100", "WB-SKU-200"],
            },
            idempotency_key="wb-batch1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["exported_count"], 1)
        self.assertEqual(out["result"]["failed_count"], 1)

    def test_selective_rows_boundary(self):
        rows = [{"sku": "A"}, {"sku": "B"}]
        self.assertEqual(len(selective_rows(all_rows=rows, selected=["A"])), 1)


class WildberriesNoBypassTests(unittest.TestCase):
    def test_separate_from_bitrix_and_onec(self):
        svc = _svc()
        self.assertNotEqual(type(svc._adapter_for_provider("wildberries")), type(svc._adapter_for_provider("bitrix")))
        self.assertNotEqual(type(svc._adapter_for_provider("wildberries")), type(svc._adapter_for_provider("onec")))


class WildberriesBAE2ETests(unittest.TestCase):
    def test_ba_price_stock_read(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Покажи цену и остаток товара WB-SKU-100 на Wildberries",
            read_only=True,
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertTrue(any(a.get("type") == "wb_price_stock" for a in ex.artifacts))

    def test_ba_price_governed_flow(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Поставь цену товара WB-SKU-100 на Wildberries 49 990 ₽",
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


class WildberriesRateLimitTests(unittest.TestCase):
    def test_rate_limit(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        svc.adapter_state("wildberries").rate_limited = True
        from integrations.activation.errors import IntegrationRateLimitedError

        with self.assertRaises(IntegrationRateLimitedError):
            svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="marketplace.wb.price.read",
                environment=ENV_FIXTURE,
                operation_class="READ",
            )


if __name__ == "__main__":
    unittest.main()
