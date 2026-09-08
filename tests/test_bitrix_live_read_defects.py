"""Regression tests for two real production defects discovered during the
required Bitrix LIVE READ verification against the self-hosted 1C-Bitrix
"Управление сайтом" install (domain panda.msk.ru).

DEFECT A -- error handler crash: ``BitrixHttpClient.call`` referenced
``ProviderErrorCategory.AUTH_FAILED``/``ProviderErrorCategory.FORBIDDEN``,
which do not exist on the canonical taxonomy (the real names are
``AUTHENTICATION_FAILED``/``AUTHORIZATION_FAILED``). Evaluating that set
literal raised ``AttributeError`` for *every* provider error that reached
that branch (i.e. anything that wasn't RATE_LIMITED/TIMEOUT), masking the
real error -- including the exact production case below (BAD_REQUEST/404).

DEFECT B -- ``LiveBitrixAdapter.read()`` called ``crm.product.list`` for
catalog reads. That method belongs to the Bitrix24 CRM product catalog,
which does not exist on this self-hosted, non-CRM install: the production
webhook only grants ``catalog`` + ``iblock`` scopes, and calling
``crm.product.list`` returned HTTP 404 ("method not found"). The correct
self-hosted catalog/iblock contract is ``catalog.product.list`` (scope
``catalog``), which requires ``filter.iblockId``.

These tests use only local fixtures (``httpx.MockTransport`` / monkeypatched
exceptions) -- no live network calls, no real webhook secret, read-only.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx

from integrations.activation.errors import (
    IntegrationAuthFailedError,
    IntegrationNotConfiguredError,
    IntegrationRateLimitedError,
    IntegrationTimeoutNormalizedError,
)
from integrations.bitrix.client import BitrixHttpClient
from integrations.bitrix.config import BitrixIntegrationConfig, load_bitrix_config
from integrations.bitrix.errors import BitrixIntegrationError
from integrations.bitrix.live_adapter import LiveBitrixAdapter
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory

WEBHOOK_URL = "https://panda.msk.ru/rest/1/fake-webhook-secret-for-tests/"


def _live_config(**overrides) -> BitrixIntegrationConfig:
    env = {"BITRIX_INTEGRATION_MODE": "LIVE", **overrides}
    return load_bitrix_config(env)


class _EnvWebhook:
    """Context manager that sets BITRIX_WEBHOOK_URL only for the test body."""

    def __enter__(self):
        self._prior = os.environ.get("BITRIX_WEBHOOK_URL")
        os.environ["BITRIX_WEBHOOK_URL"] = WEBHOOK_URL
        return self

    def __exit__(self, *exc):
        if self._prior is None:
            os.environ.pop("BITRIX_WEBHOOK_URL", None)
        else:
            os.environ["BITRIX_WEBHOOK_URL"] = self._prior


class DefectAErrorHandlerCrashTests(unittest.TestCase):
    """A provider error must always surface as a normalized integration
    error -- never as an unrelated ``AttributeError`` that masks it."""

    def setUp(self):
        self._env = _EnvWebhook()
        self._env.__enter__()
        self.client = BitrixHttpClient(config=_live_config())

    def tearDown(self):
        self._env.__exit__()

    def _raise_provider_error(self, category: ProviderErrorCategory, message: str):
        with patch.object(
            self.client._http,
            "request",
            side_effect=ProductionProviderError(category, message=message, provider_id="bitrix"),
        ):
            return self.client.call("catalog.product.list", params={})

    def test_bad_request_404_is_not_masked_by_attribute_error(self):
        """Exact production repro: a real Bitrix BAD_REQUEST/http_404 must
        raise a normalized BitrixIntegrationError, not AttributeError."""
        with self.assertRaises(BitrixIntegrationError) as ctx:
            self._raise_provider_error(ProviderErrorCategory.BAD_REQUEST, "http_404")
        self.assertNotIsInstance(ctx.exception, AttributeError)
        self.assertIn("BAD_REQUEST", str(ctx.exception.args[0] if ctx.exception.args else ""))

    def test_authentication_failed_maps_to_auth_error_not_attribute_error(self):
        with self.assertRaises(IntegrationAuthFailedError):
            self._raise_provider_error(ProviderErrorCategory.AUTHENTICATION_FAILED, "http_401")

    def test_authorization_failed_maps_to_auth_error_not_attribute_error(self):
        with self.assertRaises(IntegrationAuthFailedError):
            self._raise_provider_error(ProviderErrorCategory.AUTHORIZATION_FAILED, "http_403")

    def test_rate_limited_still_maps_correctly(self):
        with self.assertRaises(IntegrationRateLimitedError):
            self._raise_provider_error(ProviderErrorCategory.RATE_LIMITED, "rate_limited")

    def test_timeout_still_maps_correctly(self):
        with self.assertRaises(IntegrationTimeoutNormalizedError):
            self._raise_provider_error(ProviderErrorCategory.TIMEOUT, "request_timeout")

    def test_provider_unavailable_is_not_masked_by_attribute_error(self):
        with self.assertRaises(BitrixIntegrationError) as ctx:
            self._raise_provider_error(ProviderErrorCategory.PROVIDER_UNAVAILABLE, "http_503")
        self.assertNotIsInstance(ctx.exception, AttributeError)

    def test_no_category_referenced_by_client_is_missing_from_taxonomy(self):
        """Guards against reintroducing invented category names: every
        ProviderErrorCategory member referenced by the Bitrix client must
        actually exist on the canonical enum."""
        import ast
        import inspect

        import integrations.bitrix.client as client_module

        source = inspect.getsource(client_module)
        tree = ast.parse(source)
        referenced = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ProviderErrorCategory"
            ):
                referenced.add(node.attr)
        self.assertTrue(referenced, "expected at least one ProviderErrorCategory reference")
        canonical = set(ProviderErrorCategory.__members__)
        self.assertTrue(
            referenced.issubset(canonical),
            f"client.py references non-existent ProviderErrorCategory members: {referenced - canonical}",
        )


def _mock_transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class DefectBWrongMethodAndUrlTests(unittest.TestCase):
    """LiveBitrixAdapter.read() must call the correct self-hosted
    catalog/iblock REST contract, build the correct request URL from a
    full webhook base URL without duplication/truncation, and remain
    strictly read-only."""

    def setUp(self):
        self._env = _EnvWebhook()
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__()

    def _adapter(self, **config_overrides) -> LiveBitrixAdapter:
        return LiveBitrixAdapter(config=_live_config(**config_overrides))

    def test_catalog_read_uses_catalog_product_list_not_crm(self):
        """Root cause of the production HTTP 404: crm.product.list does not
        exist on this self-hosted, non-CRM install (webhook only grants
        catalog + iblock scopes). Must use catalog.product.list."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"products": [{"id": 477, "iblockId": 14, "name": "X"}]}, "total": 1})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)

        out = adapter.read(capability="cms.bitrix.catalog.read", params={})

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], WEBHOOK_URL + "catalog.product.list.json")
        self.assertNotIn("crm.product", captured["url"])
        self.assertEqual(captured["body"]["filter"], {"iblockId": 14})
        self.assertEqual(out["items"], [{"id": 477, "iblockId": 14, "name": "X"}])
        self.assertEqual(out["mode"], "LIVE")
        self.assertTrue(out["live"])

    def test_webhook_base_url_not_duplicated_or_truncated(self):
        """A full 'Вебхук для вызова REST API' base URL must produce exactly
        one occurrence of the REST path segment -- no duplication, no
        truncation of the user-id/secret path component."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"result": {"products": []}, "total": 0})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)
        adapter.read(capability="cms.bitrix.catalog.read", params={})

        self.assertEqual(captured["url"], "https://panda.msk.ru/rest/1/fake-webhook-secret-for-tests/catalog.product.list.json")
        self.assertEqual(captured["url"].count("/rest/"), 1)
        self.assertIn("fake-webhook-secret-for-tests", captured["url"])

    def test_missing_iblock_id_fails_closed_without_network_call(self):
        """filter.iblockId is a required, installation-specific parameter of
        catalog.product.list -- it must never be guessed/hardcoded. Without
        it configured, the adapter must fail closed and must NOT attempt a
        request at all."""
        called = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, json={"result": {"products": []}})

        adapter = self._adapter()  # no BITRIX_CATALOG_ID
        adapter.client._http._client = _mock_transport(handler)

        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.read(capability="cms.bitrix.catalog.read", params={})
        self.assertEqual(called["count"], 0)

    def test_order_read_operation_unaffected(self):
        """Only the demonstrated catalog defect is fixed -- sale.order.list
        for order_read is out of scope and must remain unchanged."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"result": []})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)
        out = adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "order_read"})

        self.assertEqual(captured["url"], WEBHOOK_URL + "sale.order.list.json")
        self.assertEqual(out["items"], [])

    def test_real_production_404_is_reproducible_with_old_method_and_now_uses_new_one(self):
        """Sanity: simulate the exact server-side behavior observed in
        production (crm.product.list -> 404, catalog.product.list -> 200)
        against a single fake self-hosted Bitrix, proving the fixed adapter
        picks the working method."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "crm.product.list" in str(request.url):
                return httpx.Response(404, json={"error": "ERROR_METHOD_NOT_FOUND"})
            if "catalog.product.list" in str(request.url):
                return httpx.Response(200, json={"result": {"products": [{"id": 477, "name": "Телевизор Rews-788"}]}})
            return httpx.Response(404)

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)
        out = adapter.read(capability="cms.bitrix.catalog.read", params={})
        self.assertEqual(out["items"], [{"id": 477, "name": "Телевизор Rews-788"}])

    def test_read_never_mutates(self):
        """Bounded READ verification must remain non-mutating: only POST to
        a *.list read method is issued, never a create/update/delete
        method, and write() remains blocked regardless."""
        methods_called = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods_called.append(str(request.url))
            return httpx.Response(200, json={"result": {"products": []}})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)
        adapter.read(capability="cms.bitrix.catalog.read", params={})

        for url in methods_called:
            self.assertTrue(url.endswith(".list.json"), url)
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.write(capability="cms.bitrix.catalog.write", payload={"operation": "price_update"}, idempotency_key="x")


if __name__ == "__main__":
    unittest.main()
