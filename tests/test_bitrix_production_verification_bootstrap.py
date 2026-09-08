"""Block 5.6 final defect closure — standalone LIVE Bitrix schema-binding
verification bootstrap.

Reproduces the reported production defect (a fresh
``IntegrationActivationService`` cannot resolve a LIVE Bitrix connection
for ``BitrixProductBridge.verify_schema_binding`` even though
``LiveBitrixAdapter``/``BitrixIntegrationConfig`` are already correctly
configured from protected env), proves the fix
(``integrations.bitrix.production_verification``), and proves normal
gateway/write-safety behavior is unchanged. No real network calls; HTTP is
mocked at the ``BoundedHttpClient.request`` seam so the test exercises the
*real* multi-instance gateway/adapter construction path exactly as
production does.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx

from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.activation.models import ENV_LIVE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.live_adapter import LiveBitrixAdapter
from integrations.bitrix.product_bridge import BitrixProductBridge
from integrations.bitrix.production_verification import (
    LIVE_CREDENTIAL_REF,
    ensure_live_bitrix_connection,
    run_production_schema_verification,
)
from integrations.production.http import BoundedHttpClient

FAKE_WEBHOOK_URL = "https://panda.msk.ru/rest/1/totally-fake-test-secret-never-real/"


class _LiveEnv:
    """Sets the same env shape production Railway configuration uses,
    restoring prior values on exit -- never asserts on/leaks the fake value
    used here as anything but an obviously-fake test placeholder."""

    KEYS = ("BITRIX_INTEGRATION_MODE", "BITRIX_WEBHOOK_URL", "BITRIX_CATALOG_ID", "BITRIX_OFFERS_IBLOCK_ID")

    def __enter__(self):
        self._prior = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["BITRIX_INTEGRATION_MODE"] = "LIVE"
        os.environ["BITRIX_WEBHOOK_URL"] = FAKE_WEBHOOK_URL
        os.environ["BITRIX_CATALOG_ID"] = "14"
        os.environ["BITRIX_OFFERS_IBLOCK_ID"] = "15"
        return self

    def __exit__(self, *exc):
        for k, v in self._prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _NoLiveEnv:
    def __enter__(self):
        self._prior = {k: os.environ.pop(k, None) for k in _LiveEnv.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._prior.items():
            if v is not None:
                os.environ[k] = v


def _mock_bitrix_responses(method: str, url: str, **kwargs) -> httpx.Response:
    if url.endswith("catalog.product.list.json"):
        return httpx.Response(
            200,
            json={
                "result": {
                    "products": [
                        {
                            "id": 477,
                            "iblockId": 14,
                            "name": "Телевизор Rews-788",
                            "iblockSectionId": 73,
                            "quantity": 1000,
                            "property100": {"value": "75", "valueId": "1"},
                            "property106": "banner-content",
                        }
                    ]
                }
            },
        )
    if url.endswith("catalog.section.list.json"):
        return httpx.Response(
            200,
            json={
                "result": {
                    "sections": [
                        {"id": 61, "name": "Электроника", "iblockSectionId": None},
                        {"id": 70, "name": "Телевизоры", "iblockSectionId": 61},
                        {"id": 73, "name": "Изогнутые телевизоры", "iblockSectionId": 70},
                    ]
                }
            },
        )
    if url.endswith("catalog.product.offer.list.json"):
        return httpx.Response(200, json={"result": {"offers": [{"id": 9001, "parentId": 477, "quantity": 1000}]}})
    if url.endswith("catalog.price.list.json"):
        return httpx.Response(
            200,
            json={
                "result": {
                    "prices": [
                        {"id": 1, "productId": 477, "catalogGroupId": 1, "price": "192000", "currency": "RUB"},
                        {"id": 2, "productId": 477, "catalogGroupId": 2, "price": "194000", "currency": "RUB"},
                    ]
                }
            },
        )
    raise AssertionError(f"unexpected mocked Bitrix call: {method} {url}")


class DefectReproductionTests(unittest.TestCase):
    """Reproduces the exact reported symptom -- must still reproduce
    identically for any caller that does NOT go through the new bootstrap,
    proving the fix does not weaken normal gateway behavior."""

    def test_standalone_service_cannot_resolve_live_bitrix_without_bootstrap(self):
        with _LiveEnv():
            svc = IntegrationActivationService()
            bridge = BitrixProductBridge(integration_activation=svc, environment=ENV_LIVE)
            with self.assertRaises(IntegrationNotConfiguredError) as ctx:
                bridge.verify_schema_binding(tenant_id="production", bitrix_product_id="477")
            self.assertEqual(ctx.exception.code, "cms.bitrix.catalog.read")


class EnsureLiveBitrixConnectionTests(unittest.TestCase):
    def test_fails_closed_without_live_configuration_and_registers_nothing(self):
        with _NoLiveEnv():
            svc = IntegrationActivationService()
            with self.assertRaises(IntegrationNotConfiguredError):
                ensure_live_bitrix_connection(svc, tenant_id="production")
            self.assertEqual(svc.list_connections(tenant_id="production", provider_id="bitrix"), [])

    def test_registers_verifies_and_activates_a_live_connection(self):
        with _LiveEnv():
            svc = IntegrationActivationService()
            connection_id = ensure_live_bitrix_connection(svc, tenant_id="production")
            conn = svc.get_connection(tenant_id="production", connection_id=connection_id)
            self.assertEqual(conn.provider_id, "bitrix")
            self.assertEqual(conn.environment, ENV_LIVE)
            self.assertEqual(conn.status, "ACTIVE")
            self.assertEqual(conn.credential_ref, LIVE_CREDENTIAL_REF)

    def test_is_idempotent_and_never_duplicates_connections(self):
        with _LiveEnv():
            svc = IntegrationActivationService()
            first = ensure_live_bitrix_connection(svc, tenant_id="production")
            second = ensure_live_bitrix_connection(svc, tenant_id="production")
            self.assertEqual(first, second)
            self.assertEqual(len(svc.list_connections(tenant_id="production", provider_id="bitrix")), 1)

    def test_credential_ref_is_a_name_never_a_resolvable_secret_value(self):
        """The activation service's own secret store must never hold a real
        value for this ref -- the real webhook URL only ever comes from
        protected env via LiveBitrixAdapter itself."""
        with _LiveEnv():
            svc = IntegrationActivationService()
            ensure_live_bitrix_connection(svc, tenant_id="production")
            self.assertIsNone(svc._resolve_secret("production", LIVE_CREDENTIAL_REF))

    def test_does_not_affect_other_tenants(self):
        """Scoped bootstrap, not a global auto-activation of arbitrary
        providers/tenants."""
        with _LiveEnv():
            svc = IntegrationActivationService()
            ensure_live_bitrix_connection(svc, tenant_id="production")
            self.assertEqual(svc.list_connections(tenant_id="some-other-tenant", provider_id="bitrix"), [])


class ProductionVerificationEndToEndTests(unittest.TestCase):
    def test_full_verification_succeeds_through_the_real_gateway(self):
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=_mock_bitrix_responses):
            report = run_production_schema_verification(bitrix_product_id="477")

        self.assertTrue(report["found"])
        self.assertEqual(report["identity"]["id"], 477)
        self.assertEqual(report["category"]["section_id"], 73)
        self.assertEqual([a["id"] for a in report["category"]["ancestors"]], [61, 70, 73])
        self.assertEqual(report["offers"][0]["parent_link"]["parent_product_id"], 477)
        self.assertEqual(len(report["prices"]), 2)
        self.assertFalse(report["seo"]["effective_seo_available"])
        self.assertTrue(any("warehouse_stock_unavailable" in lim for lim in report["limitations"]))

    def test_no_write_operation_is_ever_emitted_during_verification(self):
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=_mock_bitrix_responses):
            with patch.object(LiveBitrixAdapter, "write", side_effect=AssertionError("write must never be called")):
                report = run_production_schema_verification(bitrix_product_id="477")
        self.assertTrue(report["found"])

    def test_report_never_contains_the_webhook_secret_or_raw_url(self):
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=_mock_bitrix_responses):
            report = run_production_schema_verification(bitrix_product_id="477")
        serialized = json.dumps(report, default=str, ensure_ascii=False)
        self.assertNotIn(FAKE_WEBHOOK_URL, serialized)
        self.assertNotIn("totally-fake-test-secret-never-real", serialized)

    def test_reuses_existing_active_connection_across_repeated_runs(self):
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=_mock_bitrix_responses):
            svc = IntegrationActivationService()
            first = run_production_schema_verification(bitrix_product_id="477", activation=svc)
            second = run_production_schema_verification(bitrix_product_id="477", activation=svc)
        self.assertEqual(first["connection_id"], second["connection_id"])
        self.assertEqual(len(svc.list_connections(tenant_id="production", provider_id="bitrix")), 1)


if __name__ == "__main__":
    unittest.main()
