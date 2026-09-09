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

    def __init__(
        self,
        *,
        offer_should_fail: bool = False,
        price_should_fail: bool = False,
        product_error: dict | None = None,
        product_add_response_key: str | None = None,
        sections: list | None = None,
    ):
        self.calls: list[tuple[str, dict]] = []
        self.offer_should_fail = offer_should_fail
        self.price_should_fail = price_should_fail
        self.product_error = product_error
        # Lets tests reproduce the exact malformed-shape defect (a real
        # HTTP 200 whose body nests the created product under the WRONG
        # key, e.g. the pre-fix adapter's own "product" assumption) without
        # ever needing a genuine Bitrix error envelope.
        self.product_add_response_key = product_add_response_key
        # Scripted ``catalog.section.list`` result (complete-product-card
        # follow-up pass, item A) -- a list of ``{"id", "name", ...}``
        # dicts, exactly the shape ``schema.resolve_section_id`` expects.
        # Left empty by default so any test that does not care about
        # section resolution never needs to know about this.
        self.sections = sections if sections is not None else []
        self.product_add_count = 0
        self.offer_add_count = 0
        self.price_add_count = 0
        self._products_by_xml_id: dict[str, dict] = {}
        self._offers_by_parent: dict[int, dict] = {}
        self._prices: set[tuple[int, int]] = set()
        self._next_id = CREATED_PRODUCT_ID

    # Real Bitrix REST contract (apidocs.bitrix24.com): catalog.product.list
    # and catalog.product.offer.list both REQUIRE "id" AND "iblockId" in
    # ``select`` -- omitting either is documented error 200040300010
    # ("Fields id, iblockId are not specified in the selection fields"),
    # surfaced over HTTP as a 400. This is the exact real production defect
    # (request_id d5fda7ca-4915-4d75-bf61-f22ef4693f64): the idempotency
    # lookup's ``select`` omitted "iblockId". Enforcing it here means any
    # regression that drops "iblockId" from a list ``select`` again fails
    # every test in this file with the real Bitrix error shape, instead of
    # silently passing against an overly-permissive mock.
    _REQUIRED_LIST_SELECT = {
        "catalog.product.list": {"id", "iblockId"},
        "catalog.product.offer.list": {"id", "iblockId"},
    }

    def __call__(self, method: str, url: str, **kwargs) -> httpx.Response:
        body = json.loads(json.dumps(kwargs.get("json_body") or {}))
        rest_method = url.rsplit("/", 1)[-1].removesuffix(".json")
        self.calls.append((rest_method, body))
        filt = body.get("filter") or {}

        required_select = self._REQUIRED_LIST_SELECT.get(rest_method)
        if required_select:
            missing = required_select - set(body.get("select") or [])
            if missing:
                return httpx.Response(
                    400,
                    json={
                        "error": 200040300010,
                        "error_description": f"Fields {', '.join(sorted(missing))} are not specified in the selection fields",
                    },
                )

        if rest_method == "catalog.product.list":
            if "xmlId" in filt:
                match = self._products_by_xml_id.get(filt["xmlId"])
                return httpx.Response(200, json={"result": {"products": [match] if match else []}})
            if filt.get("id") is not None:
                match = next((p for p in self._products_by_xml_id.values() if p["id"] == filt["id"]), None)
                observed = None
                if match:
                    observed = {
                        "id": match["id"],
                        "iblockId": 14,
                        "name": match["name"],
                        "active": match["active"],
                        "property100": match.get("property100"),
                    }
                    if match.get(schema.SECTION_FIELD) is not None:
                        observed[schema.SECTION_FIELD] = match[schema.SECTION_FIELD]
                return httpx.Response(
                    200, json={"result": {"products": [observed] if observed else []}}
                )
            return httpx.Response(200, json={"result": {"products": list(self._products_by_xml_id.values())}})

        if rest_method == "catalog.product.add":
            self.product_add_count += 1
            if self.product_error:
                return httpx.Response(200, json=self.product_error)
            product_id = self._next_id
            self._next_id += 1
            record = {
                "id": product_id,
                "name": body["fields"]["name"],
                "active": body["fields"]["active"],
                "property100": body["fields"].get("property100"),
                schema.SECTION_FIELD: body["fields"].get(schema.SECTION_FIELD),
            }
            self._products_by_xml_id[body["fields"]["xmlId"]] = record
            # Real Bitrix REST contract (apidocs.bitrix24.com/api-reference/
            # catalog/product/catalog-product-add.html): catalog.product.add
            # nests the created product under "element", NOT "product" --
            # this is the exact real production defect
            # (product_create_malformed_response): the adapter previously
            # looked for "product" and never recognized a genuinely
            # successful HTTP 200 create.
            if self.product_add_response_key:
                return httpx.Response(
                    200, json={"result": {self.product_add_response_key: {"id": product_id, "name": record["name"], "active": record["active"]}}}
                )
            return httpx.Response(200, json={"result": {"element": {"id": product_id, "name": record["name"], "active": record["active"]}}})

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

        if rest_method == "catalog.section.list":
            return httpx.Response(200, json={"result": {"sections": self.sections}})

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
        # Deliberately NOT supplied by default any more: category/section
        # now has a real, resolvable-or-fail-closed LIVE destination (see
        # SectionAssignmentTests below) -- an arbitrary placeholder like
        # the old "CE" would now correctly fail closed (no matching
        # section) rather than silently landing in ``not_written``, which
        # would make every other, unrelated test below (offer/price/
        # purchase-price/partial-failure mechanics) also have to mock
        # catalog.section.list for no reason. Tests that care about
        # category/section pass it explicitly.
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
        # Purchase price -> native purchasingPrice/purchasingCurrency fields
        # on the SAME catalog.product.add call as the base product (Block
        # 5.6 follow-up defect closure) -- never on the offer or price call.
        self.assertEqual(product_body["fields"]["purchasingPrice"], TARGET_PURCHASE_PRICE)
        self.assertEqual(product_body["fields"]["purchasingCurrency"], "RUB")

        _, offer_body = transport.calls[3]
        self.assertEqual(offer_body["fields"]["iblockId"], 15)
        self.assertEqual(offer_body["fields"]["parentId"], CREATED_PRODUCT_ID)
        self.assertEqual(offer_body["fields"]["active"], "N")
        # SKU/article -> verified offer property 283
        self.assertEqual(offer_body["fields"]["property283"], TARGET_SKU)
        # Purchase price must never leak onto the offer create either.
        self.assertNotIn("purchasingPrice", offer_body["fields"])

        _, price_body = transport.calls[5]
        self.assertEqual(price_body["fields"]["productId"], CREATED_PRODUCT_ID)
        self.assertEqual(price_body["fields"]["catalogGroupId"], 7)
        # 6. proof retail 29990 and purchase 22513.70 remain separate --
        # retail price only ever goes through catalog.price.add, purchase
        # price only ever goes through catalog.product.add's native fields;
        # neither call's fields carry the other's value.
        self.assertEqual(price_body["fields"]["price"], TARGET_RETAIL_PRICE)
        self.assertNotIn("purchasingPrice", price_body["fields"])
        self.assertNotIn(TARGET_PURCHASE_PRICE, json.dumps(price_body))

        # EAN/category must never be guessed onto any property either.
        serialized_calls = json.dumps(transport.calls)
        self.assertNotIn(TARGET_EAN, serialized_calls)
        self.assertNotIn(TARGET_CATEGORY, serialized_calls)

        # Result-level proof that purchase price was actually written.
        self.assertTrue(result["purchase_price_written"])

    def test_fields_without_verified_destination_are_reported_not_written(self):
        """EAN still has no verified Bitrix destination on this
        installation and is never written. Purchase price now DOES have
        one (native purchasingPrice/purchasingCurrency fields, Block 5.6
        follow-up defect closure) and is written on this LIVE bridge -- it
        must no longer be reported as unwritten. (Category/section is
        covered separately below -- see SectionAssignmentTests -- since it
        now has a real, resolve-or-fail-closed destination instead of
        always being unwritten.)"""
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        not_written_fields = {item["field"] for item in result["not_written"]}
        self.assertEqual(not_written_fields, {"ean"})
        self.assertEqual(result["purchase_price_written"], True)
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


class MalformedHttp200ResponseTests(unittest.TestCase):
    """Production defect closure (product_create_malformed_response,
    request_id observed with HTTP 200 on both catalog.product.list and
    catalog.product.add, yet no product ever appeared in Bitrix): proves
    HTTP 200 is never treated as proof of success, only a concretely
    extracted id is -- and reproduces the exact real defect (the adapter
    previously looked for "product" instead of Bitrix's real "element"
    key) end to end."""

    def test_the_real_previously_wrong_response_key_reproduces_the_defect_and_fails_closed(self):
        transport = _RecordingTransport(product_add_response_key="product")
        with _LiveEnv():
            webhook_url = os.environ["BITRIX_WEBHOOK_URL"]
            with self.assertLogs("integrations.bitrix.live_adapter", level="WARNING") as log_ctx:
                with patch.object(BoundedHttpClient, "request", side_effect=transport):
                    bridge, _ = _bridge_and_activation()
                    result = execute_single_product_write(
                        bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True
                    )

        self.assertEqual(result["status"], STATUS_WRITE_FAILED)
        self.assertFalse(result["mutated"])
        self.assertEqual(result["error"], "product_create_malformed_response")
        # catalog.product.add was reached and returned HTTP 200, but no
        # offer/price step must ever be attempted without a concrete id.
        methods_called = [m for m, _ in transport.calls]
        self.assertEqual(methods_called, ["catalog.product.list", "catalog.product.add"])
        self.assertEqual(transport.offer_add_count, 0)
        self.assertEqual(transport.price_add_count, 0)
        text = format_bitrix_write_result_text(result)
        self.assertIn("product_create_malformed_response", text)
        self.assertNotIn("WRITE_VERIFIED", str(result["status"]))
        # Diagnostics closure (requirement 4): the malformed shape itself
        # -- REST method + bounded/sanitized top-level response keys -- is
        # observable in application logs, without needing the webhook URL,
        # credentials, or any other secret.
        joined_logs = " ".join(log_ctx.output)
        self.assertIn("catalog.product.add", joined_logs)
        self.assertIn("result_keys", joined_logs)
        self.assertNotIn(webhook_url, joined_logs)

    def test_a_response_with_no_result_at_all_also_fails_closed_not_verified(self):
        transport = _RecordingTransport()

        # Force a response with no usable "result" shape whatsoever --
        # simulates a genuinely empty/unexpected HTTP 200 body distinct
        # from any known-wrong key.
        real_call = transport.__call__

        def _empty_result_for_product_add(method, url, **kwargs):
            rest_method = url.rsplit("/", 1)[-1].removesuffix(".json")
            if rest_method == "catalog.product.add":
                transport.calls.append((rest_method, json.loads(json.dumps(kwargs.get("json_body") or {}))))
                transport.product_add_count += 1
                return httpx.Response(200, json={"result": {}})
            return real_call(method, url, **kwargs)

        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=_empty_result_for_product_add):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_FAILED)
        self.assertEqual(result["error"], "product_create_malformed_response")
        self.assertNotEqual(result["status"], STATUS_WRITE_VERIFIED)

    def test_the_real_documented_element_key_is_now_recognized_as_success(self):
        """The exact real successful catalog.product.add response shape
        (apidocs.bitrix24.com/api-reference/catalog/product/catalog-
        product-add.html): {"result": {"element": {"id": ..., ...}}}."""
        transport = _RecordingTransport()  # defaults to the real "element" key
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(result["bitrix_product_id"], str(CREATED_PRODUCT_ID))
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"]["name"], TARGET_TITLE)


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

    def test_production_confirmed_retail_price_type_id_1_maps_to_catalog_group_id_1(self):
        """Business owner has explicitly confirmed BITRIX_RETAIL_PRICE_TYPE_ID
        =1 (catalogGroupId 1 / BASE) is the intended retail price for THIS
        production installation -- proves the real production value works
        end to end, while remaining entirely environment-driven (never
        hardcoded into the general integration logic; see ``_LiveEnv``'s
        default of "7" in every other test in this file, which proves the
        adapter never assumes 1)."""
        transport = _RecordingTransport()
        with _LiveEnv(retail_price_type_id="1"), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        _, price_body = next((m, b) for m, b in transport.calls if m == "catalog.price.add")
        self.assertEqual(price_body["fields"]["catalogGroupId"], 1)
        self.assertEqual(price_body["fields"]["price"], TARGET_RETAIL_PRICE)


class PurchasePriceMappingTests(unittest.TestCase):
    """Block 5.6 follow-up defect closure: the LIVE READ-ONLY discovery
    proved ``purchasingPrice``/``purchasingCurrency`` are real, native
    Bitrix catalog.product fields -- this closure implements writing them
    for the controlled single-product create, independent of (and never
    substitutable for) the retail selling price."""

    def test_purchase_price_and_currency_are_mapped_onto_the_product_create_call(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertTrue(result["purchase_price_written"])
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"]["purchasingPrice"], "22513.70")
        self.assertEqual(product_body["fields"]["purchasingCurrency"], "RUB")

    def test_retail_price_remains_29990_and_catalog_group_id_remains_config_driven(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["retail_price"]["amount"], TARGET_RETAIL_PRICE)
        _, price_body = next((m, b) for m, b in transport.calls if m == "catalog.price.add")
        self.assertEqual(price_body["fields"]["price"], TARGET_RETAIL_PRICE)
        # config-driven (this test's _LiveEnv default), never hardcoded 1.
        self.assertEqual(price_body["fields"]["catalogGroupId"], 7)

    def test_purchase_price_can_never_overwrite_or_appear_in_the_retail_price_call(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        _, price_body = next((m, b) for m, b in transport.calls if m == "catalog.price.add")
        self.assertEqual(price_body["fields"]["price"], TARGET_RETAIL_PRICE)
        self.assertNotEqual(price_body["fields"]["price"], TARGET_PURCHASE_PRICE)
        self.assertNotIn("purchasingPrice", price_body["fields"])

    def test_no_purchase_price_supplied_writes_no_purchasing_fields_at_all(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(purchase_price=""), approved=True
            )

        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertFalse(result["purchase_price_written"])
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertNotIn("purchasingPrice", product_body["fields"])
        self.assertNotIn("purchasingCurrency", product_body["fields"])

    def test_malformed_purchase_price_fails_closed_before_any_http_call(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(purchase_price="not-a-number"), approved=True
            )

        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["reason"], "invalid_purchase_price")
        self.assertFalse(result.get("mutated", False))
        self.assertEqual(transport.calls, [])

    def test_negative_purchase_price_fails_closed_before_any_http_call(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(purchase_price="-500"), approved=True
            )

        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["reason"], "invalid_purchase_price")
        self.assertEqual(transport.calls, [])

    def test_ean_is_never_written_alongside_purchase_price(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertIn("ean", {item["field"] for item in result["not_written"]})
        self.assertNotIn(TARGET_EAN, json.dumps(transport.calls))

    def test_active_remains_n_even_with_purchase_price_written(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        self.assertEqual(result["active"], False)
        _, product_body = next((m, b) for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"]["active"], "N")

    def test_duplicate_idempotency_protection_remains_intact_with_purchase_price(self):
        transport = _RecordingTransport()
        fixed_key = "cbw-fixed-key-purchase-price-idempotency"
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            first = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True, idempotency_key=fixed_key
            )
            second = execute_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True, idempotency_key=fixed_key
            )

        self.assertEqual(first["status"], STATUS_WRITE_VERIFIED)
        self.assertIn(second["status"], {STATUS_WRITE_VERIFIED, "UNCHANGED"})
        self.assertEqual(transport.product_add_count, 1)

    def test_no_unrelated_unmanaged_properties_are_mutated(self):
        """Only the verified fields (name, active, BRAND/property100,
        purchasingPrice/purchasingCurrency, ARTICLE/property283, retail
        price) ever appear in any outbound Bitrix payload for this write --
        no other propertyN key is ever sent."""
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        allowed_product_fields = {"iblockId", "name", "active", "xmlId", "property100", "purchasingPrice", "purchasingCurrency"}
        allowed_offer_fields = {"iblockId", "name", "active", "parentId", "property283"}
        for method, body in transport.calls:
            fields = body.get("fields")
            if not fields:
                continue
            if method == "catalog.product.add":
                self.assertTrue(set(fields.keys()).issubset(allowed_product_fields), fields)
            elif method == "catalog.product.offer.add":
                self.assertTrue(set(fields.keys()).issubset(allowed_offer_fields), fields)


class ProductionRegression400Tests(unittest.TestCase):
    """Direct reproduction + closure of the real production defect
    (request_id d5fda7ca-4915-4d75-bf61-f22ef4693f64): the very first
    idempotency lookup, ``catalog.product.list`` filtered by ``xmlId``,
    got HTTP 400 from real Bitrix because its ``select`` omitted the
    REQUIRED ``iblockId`` field (Bitrix error 200040300010 -- "Fields id,
    iblockId are not specified in the selection fields"). ``catalog.add``
    was therefore never reached and nothing was created."""

    def test_full_write_no_longer_400s_on_the_idempotency_lookup(self):
        transport = _RecordingTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _ = _bridge_and_activation()
            result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=_request(), approved=True)

        # Before the fix, this failed at the first call with
        # "BAD_REQUEST (BitrixIntegrationError)" and catalog.product.add
        # was never reached.
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        methods_called = [m for m, _ in transport.calls]
        self.assertIn("catalog.product.add", methods_called)

        # The exact corrected request shape: "iblockId" (and "id") are now
        # present in ``select`` for both list lookups that require them.
        list_call_body = next(b for m, b in transport.calls if m == "catalog.product.list")
        self.assertIn("iblockId", list_call_body["select"])
        self.assertIn("id", list_call_body["select"])
        offer_list_body = next(b for m, b in transport.calls if m == "catalog.product.offer.list")
        self.assertIn("iblockId", offer_list_body["select"])
        self.assertIn("id", offer_list_body["select"])

    def test_a_genuine_bitrix_400_now_surfaces_the_real_error_description_not_bare_bad_request(self):
        """Diagnostics closure (requirement 7). Patches at the same layer
        real production traffic actually flows through (the underlying
        ``httpx.Client.request``, NOT ``BoundedHttpClient.request`` --
        every other test in this file mocks the latter, which bypasses
        ``BoundedHttpClient``'s own status-code handling entirely and so
        could never have caught this diagnostics regression). Reproduces
        the exact malformed request production sent: filtering
        catalog.product.list by "xmlId" with a ``select`` that omits the
        required "iblockId" field -- must no longer collapse into just
        "BAD_REQUEST"; the sanitized, bounded error/error_description
        Bitrix actually sent back must be visible in the final
        diagnostic."""
        from integrations.bitrix.client import BitrixHttpClient
        from integrations.bitrix.config import load_bitrix_config
        from integrations.bitrix.errors import BitrixIntegrationError

        def _fake_httpx_request(self, method, url, **kwargs):
            return httpx.Response(
                400,
                json={"error": 200040300010, "error_description": "Fields iblockId are not specified in the selection fields"},
            )

        with _LiveEnv(), patch.object(httpx.Client, "request", _fake_httpx_request):
            client = BitrixHttpClient(config=load_bitrix_config())
            # Deliberately reproduce the exact malformed request production
            # sent: filtering catalog.product.list by "xmlId" with a
            # ``select`` that omits the required "iblockId" field.
            with self.assertRaises(BitrixIntegrationError) as ctx:
                client.call(
                    "catalog.product.list",
                    params={"filter": {"iblockId": 14, "xmlId": "does-not-matter"}, "select": ["id", "name", "active", "xmlId"]},
                )
        message = str(ctx.exception)
        self.assertNotEqual(message, "BAD_REQUEST")
        self.assertIn("iblockId", message)
        self.assertIn("200040300010", message)


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


def _direct_write(product_extra: dict, *, idempotency_key: str = "k-complete-card", transport=None):
    """Direct, isolated ``LiveBitrixAdapter.write()`` call for one
    ``product_create`` -- bypasses ``controlled_bitrix_write``/
    ``BitrixProductBridge`` entirely (same pattern as
    ``DirectAdapterLevelTests`` above), so these tests exercise exactly
    the adapter-level field construction/validation without needing to
    also mock ``catalog.section.list`` (section RESOLUTION happens one
    layer up, in ``business_assistant.controlled_bitrix_write`` -- see
    ``tests/test_bitrix_complete_product_card_followup.py`` for that)."""
    from integrations.bitrix.live_adapter import LiveBitrixAdapter

    transport = transport or _RecordingTransport()
    with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
        adapter = LiveBitrixAdapter(secret_resolver=lambda ref: os.environ.get("BITRIX_WEBHOOK_URL"))
        product = {"title": TARGET_TITLE, "sku": TARGET_SKU, "price": {"currency": "RUB", "selling_price": TARGET_RETAIL_PRICE}}
        product.update(product_extra)
        result = adapter.write(
            capability="cms.bitrix.catalog.write",
            payload={"operation": "product_create", "product": product, "active": False},
            idempotency_key=idempotency_key,
        )
    return result, transport


class SectionAssignmentTests(unittest.TestCase):
    """Native ``iblockSectionId`` assignment (schema.py module docstring
    item A). The adapter never resolves a name -> id itself (that already
    happened one layer up); it only writes an already-resolved id and
    sanity-checks it before any HTTP call."""

    def test_supplied_section_id_is_written_as_iblockSectionId(self):
        result, transport = _direct_write({"classification": {"section_id": 70}})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"][schema.SECTION_FIELD], 70)
        self.assertEqual(result["product"]["section_id_written"], 70)

    def test_no_section_supplied_omits_the_field_entirely(self):
        result, transport = _direct_write({})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertNotIn(schema.SECTION_FIELD, product_body["fields"])
        self.assertIsNone(result["product"]["section_id_written"])

    def test_non_numeric_section_id_fails_closed_before_any_http_call(self):
        transport = _RecordingTransport()
        with self.assertRaises(Exception):
            _direct_write({"classification": {"section_id": "not-a-number"}}, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_zero_or_negative_section_id_fails_closed_before_any_http_call(self):
        for bad_id in (0, -5):
            transport = _RecordingTransport()
            with self.assertRaises(Exception):
                _direct_write({"classification": {"section_id": bad_id}}, transport=transport)
            self.assertEqual(transport.calls, [])


class PhysicalDimensionsTests(unittest.TestCase):
    """Native weight/length/width/height pass-through (schema.py module
    docstring item D) -- verbatim, no unit conversion; malformed values
    fail closed before any HTTP call; missing ones are omitted, never
    forced to 0."""

    def test_supplied_dimensions_are_written_verbatim(self):
        physical = {"weight": "12000", "length": "720", "width": "420", "height": "60"}
        result, transport = _direct_write({"physical": physical})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        for key, value in physical.items():
            self.assertEqual(product_body["fields"][key], value)
        self.assertEqual(sorted(result["product"]["physical_written"]), sorted(physical))

    def test_missing_dimensions_are_omitted_never_forced_to_zero(self):
        result, transport = _direct_write({"physical": {"weight": "12000"}})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertIn("weight", product_body["fields"])
        for key in ("length", "width", "height"):
            self.assertNotIn(key, product_body["fields"])
        self.assertEqual(result["product"]["physical_written"], ["weight"])

    def test_no_physical_data_supplied_writes_nothing_physical(self):
        _result, transport = _direct_write({})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        for key in ("weight", "length", "width", "height"):
            self.assertNotIn(key, product_body["fields"])

    def test_non_numeric_dimension_fails_closed_before_any_http_call(self):
        transport = _RecordingTransport()
        with self.assertRaises(Exception):
            _direct_write({"physical": {"weight": "heavy"}}, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_negative_dimension_fails_closed_before_any_http_call(self):
        transport = _RecordingTransport()
        with self.assertRaises(Exception):
            _direct_write({"physical": {"length": "-10"}}, transport=transport)
        self.assertEqual(transport.calls, [])


class ContentAndMediaFieldsTests(unittest.TestCase):
    """Native previewText/previewTextType/detailText/detailTextType
    (schema.py module docstring items E/F) and the confirmed
    ``{"fileData": [name, base64]}`` write shape for previewPicture/
    detailPicture."""

    def test_preview_and_detail_text_are_written(self):
        content = {"short_description": "Короткое описание", "detailed_description": "Подробное описание"}
        result, transport = _direct_write({"content": content})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"][schema.PREVIEW_TEXT_FIELD], content["short_description"])
        self.assertEqual(product_body["fields"][schema.PREVIEW_TEXT_TYPE_FIELD], "text")
        self.assertEqual(product_body["fields"][schema.DETAIL_TEXT_FIELD], content["detailed_description"])
        self.assertEqual(product_body["fields"][schema.DETAIL_TEXT_TYPE_FIELD], "text")
        self.assertEqual(sorted(result["product"]["content_written"]), sorted([schema.PREVIEW_TEXT_FIELD, schema.DETAIL_TEXT_FIELD]))

    def test_missing_content_omits_the_fields_entirely(self):
        _result, transport = _direct_write({})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        for key in (schema.PREVIEW_TEXT_FIELD, schema.DETAIL_TEXT_FIELD):
            self.assertNotIn(key, product_body["fields"])

    def test_preview_and_detail_picture_use_the_confirmed_filedata_base64_shape(self):
        media = {
            "preview_picture": {"filename": "preview.jpg", "base64": "QUJD"},
            "detail_picture": {"filename": "detail.jpg", "base64": "WFla"},
        }
        result, transport = _direct_write({"media": media})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(
            product_body["fields"][schema.PREVIEW_PICTURE_FIELD],
            {schema.PICTURE_FILE_DATA_KEY: ["preview.jpg", "QUJD"]},
        )
        self.assertEqual(
            product_body["fields"][schema.DETAIL_PICTURE_FIELD],
            {schema.PICTURE_FILE_DATA_KEY: ["detail.jpg", "WFla"]},
        )
        self.assertEqual(
            sorted(result["product"]["media_written"]), sorted([schema.PREVIEW_PICTURE_FIELD, schema.DETAIL_PICTURE_FIELD])
        )

    def test_missing_media_omits_the_fields_entirely(self):
        _result, transport = _direct_write({})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        for key in (schema.PREVIEW_PICTURE_FIELD, schema.DETAIL_PICTURE_FIELD):
            self.assertNotIn(key, product_body["fields"])

    def test_media_entry_missing_base64_or_filename_fails_closed(self):
        transport = _RecordingTransport()
        with self.assertRaises(Exception):
            _direct_write({"media": {"preview_picture": {"filename": "x.jpg"}}}, transport=transport)
        self.assertEqual(transport.calls, [])


class CharacteristicsWriteTests(unittest.TestCase):
    """Generic SAFE characteristic mapping (schema.py module docstring
    item C): only the verified ``CATALOG_CHARACTERISTICS`` keys are ever
    written to a real ``propertyN`` field; any other key is silently
    dropped by the adapter (Panda still owns the source value one layer
    up -- see ``not_written``/``_UNVERIFIED_CHARACTERISTIC`` in
    ``business_assistant.controlled_bitrix_write``)."""

    def test_verified_characteristics_are_written_as_property_fields(self):
        characteristics = {
            "screen_diagonal_cm": "81",
            "screen_resolution": "3840x2160",
            "operating_system": "webOS",
            "smart_tv_support": "Да",
            "color": "Черный",
        }
        result, transport = _direct_write({"characteristics": characteristics})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"]["property154"], "81")
        self.assertEqual(product_body["fields"]["property156"], "3840x2160")
        self.assertEqual(product_body["fields"]["property206"], "webOS")
        self.assertEqual(product_body["fields"]["property209"], "Да")
        self.assertEqual(product_body["fields"]["property246"], "Черный")
        self.assertEqual(sorted(result["product"]["characteristics_written"]), sorted(characteristics))

    def test_unverified_characteristic_keys_are_never_written(self):
        result, transport = _direct_write(
            {"characteristics": {"refresh_rate_hz": "120", "display_technology": "OLED", "model_year": "2026"}}
        )
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        # None of these keys have a verified property destination -- must
        # never appear anywhere in the outgoing fields dict.
        for key in list(product_body["fields"]):
            self.assertFalse(key.startswith("property"), f"unexpected property field written: {key}={product_body['fields'][key]}")
        self.assertEqual(result["product"]["characteristics_written"], [])

    def test_no_characteristics_supplied_writes_no_property_fields(self):
        _result, transport = _direct_write({})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        for key in list(product_body["fields"]):
            self.assertFalse(key.startswith("property"))

    def test_empty_characteristic_value_is_skipped_not_written(self):
        result, transport = _direct_write({"characteristics": {"screen_diagonal_cm": ""}})
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertNotIn("property154", product_body["fields"])
        self.assertEqual(result["product"]["characteristics_written"], [])


if __name__ == "__main__":
    unittest.main()
