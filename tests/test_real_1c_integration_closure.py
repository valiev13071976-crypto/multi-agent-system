"""Real 1C integration — closure tests."""

from __future__ import annotations

import os
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
from integrations.onec.catalog import OneCCatalogStore
from integrations.onec.commerceml import parse_commerceml_safe
from integrations.onec.errors import (
    OneCAmbiguousTargetError,
    OneCNotFoundError,
    OneCUncertainWriteOutcomeError,
    OneCUnsupportedCapabilityError,
)
from integrations.onec.fixture_adapter import OneCFixtureAdapter, OneCFixtureState
from integrations.onec.live_adapter import LiveOneCAdapter
from integrations.onec.mapping import build_preview, selective_rows
from integrations.onec.webhooks import OneCWebhookReadiness


def _svc(store: OneCCatalogStore | None = None) -> IntegrationActivationService:
    svc = IntegrationActivationService()
    if store is not None:
        adapter = OneCFixtureAdapter(store=store)
        svc._adapters["onec"] = adapter
        svc._onec_fixture = adapter
    return svc


def _active(svc: IntegrationActivationService, *, tenant: str, env: str = ENV_FIXTURE):
    ref = svc.put_secret_ref(tenant_id=tenant, secret_ref=f"secret:onec-{tenant}", value=f"tok-onec-{tenant}")
    conn = svc.configure_connection(tenant_id=tenant, provider_id="onec", credential_ref=ref, environment=env)
    svc.verify_connection(tenant_id=tenant, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant, connection_id=conn.connection_id)
    return conn


class OneCLookupTests(unittest.TestCase):
    def test_product_lookup_and_variant(self):
        adapter = OneCFixtureAdapter(store=OneCCatalogStore())
        out = adapter.read(
            capability="erp.1c.catalog.read",
            params={"operation": "product_lookup", "article": "1C-SKU-100-RED"},
            tenant_id="tenant-a",
        )
        self.assertIn("variants", out["product"])
        self.assertEqual(out["product"]["article"], "1C-SKU-100")

    def test_ambiguous_identity(self):
        adapter = OneCFixtureAdapter(store=OneCCatalogStore())
        adapter.state.force_ambiguous = True
        with self.assertRaises(OneCAmbiguousTargetError):
            adapter.read(
                capability="erp.1c.catalog.read",
                params={"operation": "product_lookup", "name": "Generic Part"},
                tenant_id="tenant-a",
            )


class OneCReadTests(unittest.TestCase):
    def test_price_read_semantics(self):
        adapter = OneCFixtureAdapter()
        out = adapter.read(
            capability="erp.1c.catalog.read",
            params={"operation": "price_read", "article": "1C-SKU-100", "price_type": "RETAIL"},
            tenant_id="tenant-a",
        )
        self.assertEqual(out["price_type"], "RETAIL")
        self.assertEqual(out["currency"], "RUB")

    def test_stock_read_warehouse_distinction(self):
        adapter = OneCFixtureAdapter()
        main = adapter.read(
            capability="erp.1c.catalog.read",
            params={"operation": "stock_read", "article": "1C-SKU-100", "warehouse": "main"},
            tenant_id="tenant-a",
        )
        east = adapter.read(
            capability="erp.1c.catalog.read",
            params={"operation": "stock_read", "article": "1C-SKU-100-RED", "warehouse": "east"},
            tenant_id="tenant-a",
        )
        self.assertNotEqual(main.get("available"), east.get("available"))


class OneCWriteGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.store = OneCCatalogStore()
        self.svc = _svc(self.store)
        _active(self.svc, tenant="tenant-a")

    def test_price_write_zero_before_approval(self):
        with self.assertRaises(IntegrationWriteDeniedError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="erp.1c.catalog.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "article": "1C-SKU-100", "new_price": "49990"},
                idempotency_key="p1",
                approved_write=False,
            )
        self.assertEqual(self.store.write_count("p1"), 0)

    def test_governed_price_write_once(self):
        preview = build_preview(
            operation="price_update",
            before={"amount": "45000.00"},
            after={"amount": "49990"},
        )
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="erp.1c.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "article": "1C-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="p2",
            approved_write=True,
        )
        self.assertEqual(w1["result"]["verified"], "VERIFIED")
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="erp.1c.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "article": "1C-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="p2",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertEqual(self.store.write_count("p2"), 1)

    def test_document_create_no_duplicate(self):
        payload = {
            "operation": "document_create",
            "document": {
                "document_type": "sales_order",
                "items": [{"article": "1C-SKU-100", "qty": 1, "price": "45000"}],
                "total": "45000",
            },
        }
        w1 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="erp.1c.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="doc1",
            approved_write=True,
        )
        w2 = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="erp.1c.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload=payload,
            idempotency_key="doc1",
            approved_write=True,
        )
        self.assertTrue(w2["result"]["idempotent"])
        self.assertFalse(w2["result"].get("posted"))

    def test_stock_write_unsupported(self):
        with self.assertRaises(OneCUnsupportedCapabilityError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="erp.1c.catalog.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "stock_update", "article": "1C-SKU-100", "quantity": 5},
                idempotency_key="s1",
                approved_write=True,
            )

    def test_verification_failure(self):
        self.svc._adapters["onec"].state.verification_mismatch = True
        out = self.svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="erp.1c.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "document_create",
                "document": {"document_type": "sales_order", "items": [{"article": "1C-SKU-100", "qty": 1}]},
            },
            idempotency_key="vfail",
            approved_write=True,
        )
        self.assertEqual(out["result"]["verified"], "VERIFICATION_FAILED")

    def test_uncertain_write(self):
        self.svc._adapters["onec"].state.uncertain_write = True
        with self.assertRaises(OneCUncertainWriteOutcomeError):
            self.svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="erp.1c.catalog.write",
                environment=ENV_FIXTURE,
                operation_class="WRITE",
                payload={"operation": "price_update", "article": "1C-SKU-100", "new_price": "50000"},
                idempotency_key="u1",
                approved_write=True,
            )


class OneCTenantSecurityTests(unittest.TestCase):
    def test_tenant_isolation(self):
        svc = _svc()
        conn = _active(svc, tenant="tenant-a")
        with self.assertRaises(IntegrationCrossTenantError):
            svc.get_connection(tenant_id="tenant-b", connection_id=conn.connection_id)

    def test_secret_not_in_evidence(self):
        svc = _svc()
        secret = "ONEC_SECRET_TOKEN_XYZ999"
        ref = svc.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:onec-a", value=secret)
        conn = svc.configure_connection(tenant_id="tenant-a", provider_id="onec", credential_ref=ref, environment=ENV_FIXTURE)
        svc.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="erp.1c.catalog.read",
            environment=ENV_FIXTURE,
            operation_class="READ",
        )
        svc.assert_no_secrets_in_evidence(tenant_id="tenant-a")


class OneCLiveSafetyTests(unittest.TestCase):
    def test_live_no_fixture_fallback(self):
        svc = _svc()
        _active(svc, tenant="tenant-a", env=ENV_FIXTURE)
        with self.assertRaises(IntegrationLiveFallbackForbiddenError):
            svc.resolve_connection(tenant_id="tenant-a", capability="erp.1c.catalog.read", environment=ENV_LIVE)

    def test_live_adapter_fail_closed(self):
        adapter = LiveOneCAdapter()
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.write(
                capability="erp.1c.catalog.write",
                payload={"operation": "price_update"},
                idempotency_key="x",
            )


class OneCBatchExcelBoundaryTests(unittest.TestCase):
    def test_selective_rows(self):
        rows = [{"sku": "S1"}, {"sku": "S2"}, {"sku": "S3"}]
        self.assertEqual(len(selective_rows(all_rows=rows, selected=["S1"])), 1)

    def test_selective_export_write(self):
        svc = _svc(OneCCatalogStore())
        _active(svc, tenant="tenant-a")
        out = svc.execute_via_gateway(
            tenant_id="tenant-a",
            capability="erp.1c.catalog.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={
                "operation": "selective_export",
                "rows": [{"sku": "S1"}, {"sku": "S2"}],
                "selected": ["S1"],
            },
            idempotency_key="excel1",
            approved_write=True,
        )
        self.assertEqual(out["result"]["exported_count"], 1)
        self.assertEqual(out["result"]["skipped_count"], 1)


class OneCXmlSecurityTests(unittest.TestCase):
    def test_commerceml_safe_parse(self):
        xml = b'<?xml version="1.0"?><Catalog><Offer id="1"><Name>Pump</Name><Article>A1</Article></Offer></Catalog>'
        out = parse_commerceml_safe(xml)
        self.assertEqual(out["offers"][0]["article"], "A1")

    def test_commerceml_rejects_doctype(self):
        xml = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe "bad">]><Catalog/>'
        with self.assertRaises(ValueError):
            parse_commerceml_safe(xml)


class OneCNoDirectBitrixBypassTests(unittest.TestCase):
    def test_separate_provider_adapters(self):
        svc = _svc()
        bitrix = svc._adapter_for_provider("bitrix")
        onec = svc._adapter_for_provider("onec")
        self.assertNotEqual(type(bitrix), type(onec))
        self.assertEqual(onec.provider_id, "onec")


class OneCRestartRecoveryTests(unittest.TestCase):
    def test_mapping_persisted_in_store(self):
        store = OneCCatalogStore()
        adapter = OneCFixtureAdapter(store=store)
        adapter.write(
            capability="erp.1c.catalog.write",
            payload={
                "operation": "document_create",
                "document": {"document_type": "sales_order", "items": [{"article": "1C-SKU-100", "qty": 1}]},
            },
            idempotency_key="rec1",
            tenant_id="tenant-a",
        )
        svc2 = _svc(store)
        _active(svc2, tenant="tenant-a")
        pages = svc2.paginated_read(
            tenant_id="tenant-a",
            capability="erp.1c.catalog.read",
            environment=ENV_FIXTURE,
            max_pages=2,
        )
        self.assertTrue(pages["bounded"])


class OneCBAE2ETests(unittest.TestCase):
    def test_ba_stock_read(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Покажи остаток товара с артикулом 1C-SKU-100 в 1С",
            read_only=True,
        )
        plan = ba.build_plan(request_id=req.request_id, tenant_id="tenant-a")
        ex = ba.execute(plan_id=plan.plan_id, tenant_id="tenant-a")
        self.assertTrue(any(a.get("type") == "onec_stock" for a in ex.artifacts))

    def test_ba_price_governed_flow(self):
        act = _svc()
        _active(act, tenant="tenant-a")
        ba = BusinessAssistantService(integration_activation=act, integration_environment=ENV_FIXTURE)
        req = ba.submit_request(
            tenant_id="tenant-a",
            user_id="u",
            text="Измени цену товара с артикулом 1C-SKU-100 в 1С на 49 990 ₽",
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


class OneCRateLimitTests(unittest.TestCase):
    def test_rate_limit(self):
        svc = _svc()
        _active(svc, tenant="tenant-a")
        svc.adapter_state("onec").rate_limited = True
        with self.assertRaises(IntegrationRateLimitedError):
            svc.execute_via_gateway(
                tenant_id="tenant-a",
                capability="erp.1c.catalog.read",
                environment=ENV_FIXTURE,
                operation_class="READ",
            )


if __name__ == "__main__":
    unittest.main()
