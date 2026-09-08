"""First controlled production Bitrix write — real LIVE CREATE implementation.

Removes the ``bitrix_live_write_blocked_engineering`` placeholder in
``LiveBitrixAdapter.write()`` ONLY by implementing the minimum real LIVE
REST mutations ``controlled_bitrix_write.execute_single_product_write``
needs for its one governed ``product_create`` operation: base product
(``catalog.product.add``), the offer/SKU carrying the verified ARTICLE
property (``catalog.product.offer.add``), and the retail selling price
(``catalog.price.add``) -- reusing the exact same transport
(``BitrixHttpClient``/``BoundedHttpClient``), auth (webhook credential
resolution), and error normalization every LIVE read already uses. No
second HTTP client, no parallel integration architecture.

Every test below runs against a mocked HTTP transport
(``BoundedHttpClient.request`` patched, exactly like
``tests/test_bitrix_production_verification_bootstrap.py`` and
``tests/test_bitrix_production_schema_binding.py`` already do for LIVE
reads) -- zero real network calls, zero real Bitrix mutations, from this
file or from Cursor, at any point.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx

from business_assistant.controlled_bitrix_write import (
    STATUS_EXISTING_PRODUCT_FOUND,
    STATUS_WRITE_FAILED,
    STATUS_WRITE_PARTIAL_FAILURE,
    STATUS_WRITE_VERIFIED,
    SingleProductWriteRequest,
    execute_single_product_write,
    format_bitrix_write_result_text,
)
from integrations.activation.errors import IntegrationNotConfiguredError, IntegrationWriteDeniedError
from integrations.activation.models import ENV_LIVE, OP_WRITE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix import schema
from integrations.bitrix.catalog import BitrixCatalogStore
from integrations.bitrix.product_bridge import BitrixProductBridge
from integrations.production.http import BoundedHttpClient

TARGET_TENANT = "tenant-real-customer"
TARGET_TITLE = "Телевизор LG 32LQ63006LA.ARUG"
TARGET_SKU = "32LQ63006LA.ARUG"
TARGET_EAN = "8806096259955"
TARGET_BRAND = "LG"
TARGET_PURCHASE_PRICE = "22513.70"
TARGET_RETAIL_PRICE = "29990"
TARGET_CATEGORY = "CE"

CREATED_PRODUCT_ID = 501
CREATED_OFFER_ID = 9101


class _LiveEnv:
    """Same env shape production Railway configuration uses; includes the
    new ``BITRIX_RETAIL_PRICE_TYPE_ID`` this PR adds (fail-closed if
    absent -- see ``FailClosedWithoutRetailPriceTypeTests`` below)."""

    KEYS = (
        "BITRIX_INTEGRATION_MODE",
        "BITRIX_WEBHOOK_URL",
        "BITRIX_CATALOG_ID",
        "BITRIX_OFFERS_IBLOCK_ID",
        "BITRIX_RETAIL_PRICE_TYPE_ID",
    )

    def __init__(self, *, retail_price_type_id: str | None = "7"):
        self._retail_price_type_id = retail_price_type_id

    def __enter__(self):
        self._prior = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["BITRIX_INTEGRATION_MODE"] = "LIVE"
        os.environ["BITRIX_WEBHOOK_URL"] = "https://panda.msk.ru/rest/1/totally-fake-test-secret-never-real/"
        os.environ["BITRIX_CATALOG_ID"] = "14"
        os.environ["BITRIX_OFFERS_IBLOCK_ID"] = "15"
        if self._retail_price_type_id is None:
            os.environ.pop("BITRIX_RETAIL_PRICE_TYPE_ID", None)
        else:
            os.environ["BITRIX_RETAIL_PRICE_TYPE_ID"] = self._retail_price_type_id
        return self

    def __exit__(self, *exc):
        for k, v in self._prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _RecordingTransport:
    """Records every mocked HTTP call (method name inferred from the URL,
    plus the exact JSON body sent) and answers with a scripted response per
    Bitrix REST method -- lets tests assert on the EXACT payload Panda
    would send to real Bitrix without ever making a real call.

    Maintains enough persistent, in-memory state (products by xmlId,
    offers by parent, prices by product+group) to faithfully simulate a
    REAL Bitrix installation across multiple ``execute_single_product_write``
    calls -- this is essential for the idempotency/retry tests, since
    ``LiveBitrixAdapter`` deliberately checks-before-creating against
    exactly this kind of durable external state (see its own docstring:
    a fresh adapter instance is constructed per call, so it can never rely
    on its own memory across calls)."""

    def __init__(self, *, offer_should_fail: bool = False, price_should_fail: bool = False, product_error: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.offer_should_fail = offer_should_fail
        self.price_should_fail = price_should_fail
        self.product_error = product_error
        self.product_add_count = 0
        self.offer_add_count = 0
        self.price_add_count = 0
        self._products_by_xml_id: dict[str, dict] = {}
        self._offers_by_parent: dict[int, dict] = {}
        self._prices: set[tuple[int, int]] = set()
        self._next_id = CREATED_PRODUCT_ID

    def __call__(self, method: str, url: str, **kwargs) -> httpx.Response:
        body = json.loads(json.dumps(kwargs.get("json_body") or {}))
        rest_method = url.rsplit("/", 1)[-1].removesuffix(".json")
        self.calls.append((rest_method, body))
        filt = body.get("filter") or {}

        if rest_method == "catalog.product.list":
            if "xmlId" in filt:
                match = self._products_by_xml_id.get(filt["xmlId"])
                return httpx.Response(200, json={"result": {"products": [match] if match else []}})
            if filt.get("id") is not None:
                match = next((p for p in self._products_by_xml_id.values() if p["id"] == filt["id"]), None)
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "products": [
                                {
                                    "id": match["id"],
                                    "iblockId": 14,
                                    "name": match["name"],
                                    "active": match["active"],
                                    "property100": match.get("property100"),
                                }
                            ]
                            if match
                            else []
                        }
                    },
                )
            return httpx.Response(200, json={"result": {"products": list(self._products_by_xml_id.values())}})

        if rest_method == "catalog.product.add":
            self.product_add_count += 1
            if self.product_error:
                return httpx.Response(200, json=self.product_error)
            product_id = self._next_id
            self._next_id += 1
            record = {"id": product_id, "name": body["fields"]["name"], "active": body["fields"]["active"], "property100": body["fields"].get("property100")}
            self._products_by_xml_id[body["fields"]["xmlId"]] = record
            return httpx.Response(200, json={"result": {"product": {"id": product_id, "name": record["name"], "active": record["active"]}}})

        if rest_method == "catalog.product.offer.list":
            parent_id = filt.get(schema.CML2_LINK_REST_FIELD)
            match = self._offers_by_parent.get(parent_id)
            return httpx.Response(200, json={"result": {"offers": [match] if match else []}})

        if rest_method == "catalog.product.offer.add":
            self.offer_add_count += 1
            if self.offer_should_fail:
                return httpx.Response(500)
            offer_id = CREATED_OFFER_ID
            parent_id = body["fields"]["parentId"]
            self._offers_by_parent[parent_id] = {"id": offer_id, schema.CML2_LINK_REST_FIELD: parent_id}
            return httpx.Response(200, json={"result": {"offer": {"id": offer_id, "parentId": parent_id}}})

        if rest_method == "catalog.price.list":
            key = (filt.get("productId"), filt.get("catalogGroupId"))
            return httpx.Response(200, json={"result": {"prices": [{"id": 1, "productId": key[0], "catalogGroupId": key[1]}] if key in self._prices else []}})

        if rest_method == "catalog.price.add":
            self.price_add_count += 1
            if self.price_should_fail:
                return httpx.Response(500)
            self._prices.add((body["fields"]["productId"], body["fields"]["catalogGroupId"]))
            return httpx.Response(200, json={"result": {"price": {"id": 1, "productId": body["fields"]["productId"]}}})

        raise AssertionError(f"unexpected mocked Bitrix call: {method} {url}")


def _bridge_and_activation() -> tuple[BitrixProductBridge, IntegrationActivationService]:
    activation = IntegrationActivationService()
    # A dedicated, per-test catalog store -- ``BitrixProductBridge``
    # defaults to the shared ``GLOBAL_BITRIX_CATALOG`` singleton, which
    # would otherwise leak duplicate-detection state between these tests.
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_LIVE, store=BitrixCatalogStore())
    return bridge, activation


def _request(**overrides) -> SingleProductWriteRequest:
    base = dict(
        tenant_id=TARGET_TENANT,
        title=TARGET_TITLE,
        sku=TARGET_SKU,
        retail_price=TARGET_RETAIL_PRICE,
        ean=TARGET_EAN,
        category_source=TARGET_CATEGORY,
        brand=TARGET_BRAND,
        purchase_price=TARGET_PURCHASE_PRICE,
    )
    base.update(overrides)
    return SingleProductWriteRequest(**base)


class CorrectVerifiedCreatePayloadTests(unittest.TestCase):
    def test_full_success_sends_exactly_the_verified_fields_and_performs_read_back(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(result["bitrix_product_id"], str(CREATED_PRODUCT_ID))

        methods_called = [m for m, _ in transport.calls]
        self.assertEqual(
            methods_called,
            [
                "catalog.product.list",  # idempotency check by xmlId -- not found
                "catalog.product.add",
                "catalog.product.offer.list",  # idempotency check by parent -- not found
                "catalog.product.offer.add",
                "catalog.price.list",  # idempotency check by product+group -- not found
                "catalog.price.add",
                "catalog.product.list",  # independent read-back
            ],
        )

        _, product_body = transport.calls[1]
        self.assertEqual(product_body["fields"]["iblockId"], 14)
        self.assertEqual(product_body["fields"]["name"], TARGET_TITLE)
        # 5. proof active=false
        self.assertEqual(product_body["fields"]["active"], "N")
        # BRAND -> verified property 100
        self.assertEqual(product_body["fields"]["property100"], TARGET_BRAND)

        _, offer_body = transport.calls[3]
        self.assertEqual(offer_body["fields"]["iblockId"], 15)
        self.assertEqual(offer_body["fields"]["parentId"], CREATED_PRODUCT_ID)
        self.assertEqual(offer_body["fields"]["active"], "N")
        # SKU/article -> verified offer property 283
        self.assertEqual(offer_body["fields"]["property283"], TARGET_SKU)

        _, price_body = transport.calls[5]
        self.assertEqual(price_body["fields"]["productId"], CREATED_PRODUCT_ID)
        self.assertEqual(price_body["fields"]["catalogGroupId"], 7)
        # 6. proof retail 29990 and purchase 22513.70 remain separate
        self.assertEqual(price_body["fields"]["price"], TARGET_RETAIL_PRICE)

        # Purchase price must never appear in ANY outbound Bitrix payload.
        serialized_calls = json.dumps(transport.calls)
        self.assertNotIn(TARGET_PURCHASE_PRICE, serialized_calls)
        # EAN/category must never be guessed onto any property either.
        self.assertNotIn(TARGET_EAN, serialized_calls)
        self.assertNotIn(TARGET_CATEGORY, serialized_calls)

    def test_fields_without_verified_destination_are_reported_not_written(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        not_written_fields = {item["field"] for item in result["not_written"]}
        self.assertEqual(not_written_fields, {"ean", "purchase_price", "category"})
        self.assertEqual(result["purchase_price_written"], False)
        self.assertEqual(result["active"], False)
        self.assertEqual(result["published"], False)

    def test_read_back_verifies_name_and_active_false(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertTrue(result["read_back"]["matches"])
        self.assertEqual(result["read_back"]["observed"]["name"], TARGET_TITLE)
        self.assertEqual(result["read_back"]["observed"]["active"], False)


class GovernanceUnchangedTests(unittest.TestCase):
    def test_no_approval_means_zero_http_calls(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=False)
        self.assertEqual(result["status"], "APPROVAL_REQUIRED")
        self.assertFalse(result["mutated"])
        self.assertEqual(transport.calls, [])

    def test_missing_idempotency_key_means_zero_http_calls(self):
        """Direct proof of the pre-existing, unchanged Block 5.4 gate
        (IntegrationActivationService.execute_via_gateway) this write path
        still routes through -- not a new/duplicated check."""
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, activation = _bridge_and_activation()
            bridge.ensure_live_connection_ready(tenant_id=TARGET_TENANT)
            with self.assertRaises(IntegrationWriteDeniedError):
                activation.execute_via_gateway(
                    tenant_id=TARGET_TENANT,
                    capability="cms.bitrix.catalog.write",
                    environment=ENV_LIVE,
                    operation_class=OP_WRITE,
                    payload={"operation": "product_create", "product": {"title": TARGET_TITLE, "sku": TARGET_SKU}},
                    idempotency_key="",
                    approved_write=True,
                )
        self.assertEqual(transport.calls, [])

    def test_duplicate_sku_prevents_create_with_zero_http_calls(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            # Seed the LOCAL duplicate-detection store with an existing
            # product under the same SKU for this tenant -- plan_sync's
            # duplicate check is entirely local, so this must short-circuit
            # before any real Bitrix HTTP call is ever attempted.
            bridge._store.create_product(
                tenant_id=TARGET_TENANT, payload={"name": "Existing TV", "article": TARGET_SKU, "active": True}
            )
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)
        self.assertEqual(result["status"], STATUS_EXISTING_PRODUCT_FOUND)
        self.assertFalse(result["mutated"])
        self.assertEqual(transport.calls, [])


class BitrixApiFailureTests(unittest.TestCase):
    def test_bitrix_rest_error_is_surfaced_not_collapsed_to_fake_success(self):
        transport = _RecordingTransport(product_error={"error": "IBLOCK_ELEMENT_ADD_ERROR", "error_description": "Не удалось создать элемент"})
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_FAILED)
        self.assertFalse(result["mutated"])
        text = format_bitrix_write_result_text(result)
        self.assertIn("не удалась", text)
        self.assertNotIn("Товар создан", text)


class PartialFailureAndResumableRetryTests(unittest.TestCase):
    def test_partial_failure_after_product_creation_reports_created_id_and_failed_step(self):
        transport = _RecordingTransport(offer_should_fail=True)
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_PARTIAL_FAILURE)
        self.assertTrue(result["mutated"])
        self.assertEqual(result["bitrix_product_id"], str(CREATED_PRODUCT_ID))
        self.assertEqual(result["failed_step"], "offer_create")
        self.assertEqual(transport.product_add_count, 1)
        text = format_bitrix_write_result_text(result)
        self.assertIn("ЧАСТИЧНАЯ ОШИБКА", text)
        self.assertIn(str(CREATED_PRODUCT_ID), text)

    def test_retry_with_same_idempotency_key_never_recreates_product_and_completes(self):
        transport = _RecordingTransport(offer_should_fail=True)
        fixed_key = "cbw-fixed-test-key-for-resume"
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            first = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True, idempotency_key=fixed_key
            )
            self.assertEqual(first["status"], STATUS_WRITE_PARTIAL_FAILURE)
            self.assertEqual(transport.product_add_count, 1)

            # Fix the transient failure and retry with the SAME key.
            transport.offer_should_fail = False
            second = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True, idempotency_key=fixed_key
            )

        self.assertEqual(second["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(second["bitrix_product_id"], str(CREATED_PRODUCT_ID))
        # The product-create REST call must NEVER be repeated -- only ever
        # called once across both attempts, proving the retry resumed from
        # the already-created product instead of duplicating it. The
        # offer-create call is legitimately attempted twice (the first
        # attempt genuinely failed and created nothing), but never
        # duplicates the underlying offer once it succeeds.
        self.assertEqual(transport.product_add_count, 1)
        self.assertEqual(transport.offer_add_count, 2)
        self.assertEqual(transport.price_add_count, 1)

    def test_price_step_failure_reports_partial_failure_with_offer_already_created(self):
        transport = _RecordingTransport(price_should_fail=True)
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_PARTIAL_FAILURE)
        self.assertEqual(result["failed_step"], "price_create")
        self.assertEqual(result["bitrix_product_id"], str(CREATED_PRODUCT_ID))
        self.assertEqual(transport.offer_add_count, 1)

    def test_fully_successful_prior_write_short_circuits_on_replay_with_zero_new_http_calls(self):
        transport = _RecordingTransport()
        fixed_key = "cbw-fixed-test-key-full-success-replay"
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            first = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True, idempotency_key=fixed_key
            )
            self.assertEqual(first["status"], STATUS_WRITE_VERIFIED)

            second = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True, idempotency_key=fixed_key
            )

        # sync_product's own duplicate/idempotency short-circuit at
        # BitrixCatalogStore level (via bind_mapping) reports UNCHANGED, or
        # LiveBitrixAdapter's own idempotency cache replays WRITE_ACCEPTED
        # directly -- either way, no NEW product/offer/price create call.
        self.assertEqual(transport.product_add_count, 1)
        self.assertEqual(transport.offer_add_count, 1)
        self.assertEqual(transport.price_add_count, 1)
        self.assertIn(second["status"], {STATUS_WRITE_VERIFIED, "UNCHANGED"})


class FailClosedWithoutRetailPriceTypeTests(unittest.TestCase):
    def test_missing_retail_price_type_id_fails_closed_never_guesses_a_catalog_group_id(self):
        transport = _RecordingTransport()
        with _LiveEnv(retail_price_type_id=None), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        # Product + offer succeed (their destinations ARE verified/configured);
        # only the price step is blocked, and it fails closed rather than
        # guessing catalogGroupId=1.
        self.assertEqual(result["status"], STATUS_WRITE_PARTIAL_FAILURE)
        self.assertEqual(result["failed_step"], "price_create")
        self.assertEqual(result["error"], "bitrix_retail_price_type_id_not_configured")
        self.assertEqual(transport.price_add_count, 0)


class DirectAdapterLevelTests(unittest.TestCase):
    """Lower-level proof that every other LIVE write operation remains the
    pre-existing, documented placeholder -- only product_create changed."""

    def test_every_non_create_operation_still_raises_the_documented_placeholder(self):
        from integrations.bitrix.live_adapter import LiveBitrixAdapter

        with _LiveEnv():
            adapter = LiveBitrixAdapter(secret_resolver=lambda ref: os.environ.get("BITRIX_WEBHOOK_URL"))
            for operation in ("product_update", "price_update", "stock_update", "media_attach", "seo_update", "publish"):
                with self.assertRaises(IntegrationNotConfiguredError) as ctx:
                    adapter.write(
                        capability="cms.bitrix.catalog.write",
                        payload={"operation": operation},
                        idempotency_key=f"k-{operation}",
                    )
                self.assertEqual(ctx.exception.code, "bitrix_live_write_blocked_engineering")


if __name__ == "__main__":
    unittest.main()
