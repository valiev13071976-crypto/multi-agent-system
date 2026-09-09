"""Block 5.6 — real panda.msk.ru production schema binding tests.

Covers: catalog IBLOCK 14 mapping, offers IBLOCK 15 mapping, the
CML2_LINK(279)/``parentId`` parent relationship, dynamic (non-hardcoded)
category/section resolution, property metadata/value/ownership mapping,
BRAND semantics, price-type preservation, total-quantity vs
store-inventory distinction, SEO inherited/effective classification,
Aspro ownership, missing-optional-data handling, and fail-closed behavior
for required config -- all against local fixtures / mocked HTTP only.
"""

from __future__ import annotations

import json
import os
import unittest

import httpx

from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.bitrix import schema
from integrations.bitrix.config import load_bitrix_config
from integrations.bitrix.errors import BitrixValidationError
from integrations.bitrix.live_adapter import LiveBitrixAdapter
from integrations.bitrix.product_bridge import BitrixProductBridge

WEBHOOK_URL = "https://panda.msk.ru/rest/1/fake-webhook-secret-for-tests/"


def _live_config(**overrides) -> "object":
    env = {"BITRIX_INTEGRATION_MODE": "LIVE", **overrides}
    return load_bitrix_config(env)


class _EnvWebhook:
    def __enter__(self):
        self._prior = os.environ.get("BITRIX_WEBHOOK_URL")
        os.environ["BITRIX_WEBHOOK_URL"] = WEBHOOK_URL
        return self

    def __exit__(self, *exc):
        if self._prior is None:
            os.environ.pop("BITRIX_WEBHOOK_URL", None)
        else:
            os.environ["BITRIX_WEBHOOK_URL"] = self._prior


def _mock_transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class OwnershipClassificationTests(unittest.TestCase):
    def test_every_known_property_has_a_valid_ownership_label(self):
        for binding in schema.CATALOG_PRODUCT_PROPERTIES + schema.OFFER_PROPERTIES:
            self.assertIn(binding.ownership, schema.OWNERSHIP_LABELS, binding.code)

    def test_brand_is_panda_managed(self):
        self.assertEqual(schema.catalog_property(code="BRAND").ownership, schema.PANDA_MANAGED)

    def test_known_aspro_banner_fields_are_aspro_managed(self):
        for code in (
            "BNR_TOP_UNDER_HEADER",
            "BNR_TOP",
            "BNR_TOP_IMG",
            "BNR_TOP_BG",
            "BNR_TOP_COLOR",
            "BUTTON1TEXT",
            "BUTTON1LINK",
            "BUTTON1TARGET",
            "BUTTON1CLASS",
            "BUTTON1COLOR",
        ):
            self.assertEqual(schema.catalog_property(code=code).ownership, schema.ASPRO_MANAGED, code)

    def test_cml2_link_is_read_only_identity_relation_not_writable(self):
        binding = schema.offer_property(code="CML2_LINK")
        self.assertEqual(binding.property_id, schema.CML2_LINK_PROPERTY_ID)
        self.assertEqual(binding.ownership, schema.READ_ONLY)

    def test_unknown_property_defaults_to_unmanaged_preserve(self):
        """A property this schema binding does not recognize must never be
        assumed Panda-writable -- fail conservative, not permissive."""
        self.assertEqual(schema.property_ownership("SOME_UNKNOWN_FUTURE_PROPERTY"), schema.UNMANAGED_PRESERVE)

    def test_derived_price_rollups_are_not_panda_managed(self):
        for code in ("MINIMUM_PRICE", "MAXIMUM_PRICE", "IN_STOCK"):
            self.assertEqual(schema.catalog_property(code=code).ownership, schema.DERIVED)


class PropertyValueUnwrapTests(unittest.TestCase):
    """Block 5.6 follow-up defect closure: LIVE Bitrix wraps most non-
    boolean custom property values as ``{"value": ..., "valueId": ...}``
    (or a list of such envelopes for multi-value properties) instead of a
    bare scalar. ``schema.unwrap_property_value`` must extract the real
    value in every shape while staying backward compatible with the bare
    scalars fixtures/older responses already use."""

    def test_scalar_value_passes_through_unchanged(self):
        self.assertEqual(schema.unwrap_property_value("Y"), "Y")
        self.assertEqual(schema.unwrap_property_value(1000), 1000)

    def test_wrapped_single_value_is_unwrapped(self):
        self.assertEqual(schema.unwrap_property_value({"value": "87", "valueId": "2585"}), "87")

    def test_wrapped_multi_value_list_is_unwrapped_element_by_element(self):
        wrapped = [
            {"value": "1", "valueId": "621"},
            {"value": "2", "valueId": "622"},
            {"value": "4", "valueId": "623"},
        ]
        self.assertEqual(schema.unwrap_property_value(wrapped), ["1", "2", "4"])

    def test_null_or_missing_property_stays_none(self):
        self.assertIsNone(schema.unwrap_property_value(None))

    def test_image_reference_objects_are_not_mistaken_for_the_envelope(self):
        """File/image reference dicts (previewPicture/detailPicture, and the
        inner value of a wrapped MORE_PHOTO/280 entry) have no "value" key
        and must pass through unchanged."""
        image_ref = {"id": "689", "url": "/rest/catalog.product.download?...", "urlMachine": "..."}
        self.assertEqual(schema.unwrap_property_value(image_ref), image_ref)

    def test_wrapped_multi_value_file_property_unwraps_to_the_inner_file_objects(self):
        """MORE_PHOTO/280-shaped live data: a list of {"value": <file ref
        object>, "valueId": ...} envelopes."""
        wrapped_gallery = [
            {"value": {"id": "2369", "url": "/rest/x?fileId=2369"}, "valueId": "10332"},
            {"value": {"id": "2370", "url": "/rest/x?fileId=2370"}, "valueId": "10333"},
        ]
        unwrapped = schema.unwrap_property_value(wrapped_gallery)
        self.assertEqual(unwrapped, [{"id": "2369", "url": "/rest/x?fileId=2369"}, {"id": "2370", "url": "/rest/x?fileId=2370"}])

    def test_brand_mapping_unwraps_to_scalar_via_map_catalog_product(self):
        item = {
            "id": 118,
            "iblockId": 14,
            "name": "Test product",
            "property100": {"value": "74", "valueId": "619"},
        }
        mapped = schema.map_catalog_product(item)
        self.assertEqual(mapped["brand"]["value"], "74")
        self.assertNotIsInstance(mapped["brand"]["value"], dict)

    def test_article_mapping_unwraps_to_scalar_via_map_offer(self):
        offer_item = {
            "id": 621,
            "iblockId": 15,
            "name": "Test offer",
            "parentId": 169,
            "property283": {"value": "W324R5Y-36", "valueId": "10335"},
        }
        mapped = schema.map_offer(offer_item, parent_product_id="169")
        self.assertEqual(mapped["identity"]["article"], "W324R5Y-36")
        self.assertNotIsInstance(mapped["identity"]["article"], dict)

    def test_missing_brand_property_maps_to_none_not_a_dict(self):
        mapped = schema.map_catalog_product({"id": 1, "iblockId": 14, "name": "No brand"})
        self.assertIsNone(mapped["brand"]["value"])


class CatalogProductMappingTests(unittest.TestCase):
    def test_maps_identity_category_content_brand_characteristics_aspro_stock(self):
        item = {
            "id": 477,
            "iblockId": 14,
            "name": "Телевизор Rews-788",
            "active": "Y",
            "xmlId": "477",
            "code": "televizor-rews-788",
            "iblockSectionId": 73,
            "quantity": 1000,
            "previewText": "preview",
            "previewPicture": {"id": "1"},
            "detailText": "detail",
            "detailPicture": {"id": "2"},
            "property100": {"value": "75", "valueId": "1"},  # BRAND -> Misterio (id 75)
            "property106": "banner-under-header",  # Aspro banner field
            "property97": "180000",  # MINIMUM_PRICE (derived)
            "property999": "unrecognized-value",  # not in our known registry
        }
        mapped = schema.map_catalog_product(item)

        self.assertEqual(mapped["identity"], {"id": 477, "iblock_id": 14, "name": "Телевизор Rews-788", "active": "Y", "xml_id": "477", "code": "televizor-rews-788"})
        self.assertEqual(mapped["category"]["section_id"], 73)
        self.assertEqual(mapped["content"]["preview_text"], "preview")
        self.assertEqual(mapped["content"]["detail_text"], "detail")
        # LIVE Bitrix wraps this custom property as {"value": ..., "valueId":
        # ...} -- the mapping must unwrap it to the real scalar brand value,
        # never expose the whole envelope.
        self.assertEqual(mapped["brand"], {"value": "75", "ownership": schema.PANDA_MANAGED})
        self.assertEqual(mapped["aspro"]["BNR_TOP_UNDER_HEADER"], "banner-under-header")
        self.assertEqual(mapped["stock"], {"total_quantity": 1000, "warehouse_stock": None})

        by_code = {c["code"]: c for c in mapped["characteristics"] if c["known"]}
        self.assertEqual(by_code["MINIMUM_PRICE"]["ownership"], schema.DERIVED)
        unknown = [c for c in mapped["characteristics"] if not c["known"]]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["property_id"], 999)
        self.assertEqual(unknown[0]["ownership"], schema.UNMANAGED_PRESERVE)

    def test_missing_optional_fields_do_not_crash_or_fabricate(self):
        mapped = schema.map_catalog_product({"id": 1, "iblockId": 14, "name": "Bare product"})
        self.assertIsNone(mapped["category"]["section_id"])
        self.assertIsNone(mapped["brand"]["value"])
        self.assertEqual(mapped["characteristics"], [])
        self.assertEqual(mapped["aspro"], {})


class DynamicCategoryResolutionTests(unittest.TestCase):
    """Category resolution must walk the REAL section tree returned by the
    read, never assume a fixed hierarchy (spec: no 'everything is TV
    section 70' hardcoding)."""

    def test_resolves_real_tv_subcategory_ancestor_chain(self):
        sections_by_id = {
            61: {"id": 61, "name": "Электроника", "iblockSectionId": None},
            70: {"id": 70, "name": "Телевизоры", "iblockSectionId": 61},
            73: {"id": 73, "name": "Изогнутые телевизоры", "iblockSectionId": 70},
        }
        chain = schema.resolve_section_ancestors(73, sections_by_id)
        self.assertEqual([c["id"] for c in chain], [61, 70, 73])

    def test_resolves_a_completely_different_unrelated_hierarchy_the_same_way(self):
        """Same function, different real category tree entirely -- proves
        no TV-specific assumption is baked into the resolver itself."""
        sections_by_id = {
            5: {"id": 5, "name": "Велосипеды", "iblockSectionId": None},
            9: {"id": 9, "name": "Городские велосипеды", "iblockSectionId": 5},
        }
        chain = schema.resolve_section_ancestors(9, sections_by_id)
        self.assertEqual([c["id"] for c in chain], [5, 9])

    def test_unknown_section_id_resolves_to_empty_chain_not_fabricated(self):
        self.assertEqual(schema.resolve_section_ancestors(999999, {}), [])


class OfferParentLinkTests(unittest.TestCase):
    """Offer -> parent product relationship via CML2_LINK(279)/parentId --
    never assumes offer id == product id."""

    def test_maps_offer_with_parent_link_and_variant_attributes(self):
        offer_item = {
            "id": 9001,
            "iblockId": 15,
            "name": "Телевизор Rews-788 (offer)",
            "active": "Y",
            "xmlId": "9001",
            "parentId": 477,
            "quantity": 1000,
            "property283": {"value": "REWS-788-BLK"},  # ARTICLE
        }
        mapped = schema.map_offer(offer_item, parent_product_id="477")
        self.assertNotEqual(mapped["identity"]["id"], 477)  # offer id != product id
        self.assertEqual(mapped["parent_link"]["parent_product_id"], 477)
        self.assertEqual(mapped["parent_link"]["property_id"], 279)
        self.assertEqual(mapped["parent_link"]["property_code"], "CML2_LINK")
        self.assertTrue(mapped["parent_link"]["matches_queried_parent"])
        # LIVE Bitrix wraps ARTICLE the same way -- must unwrap to the real
        # scalar SKU string, never expose the envelope.
        self.assertEqual(mapped["identity"]["article"], "REWS-788-BLK")

    def test_maps_offer_with_live_wrapped_parent_id_envelope(self):
        """LIVE READ-ONLY discovery follow-up: ``parentId`` itself comes back
        wrapped in the same ``{"value": ..., "valueId": ...}`` envelope as
        any other custom property on this installation (it is backed by
        CML2_LINK/279) -- never a bare scalar. Must still resolve/compare
        correctly, not silently mismatch a dict against a string."""
        offer_item = {
            "id": 568,
            "iblockId": 15,
            "name": "Offer with wrapped parentId",
            "parentId": {"value": "169", "valueId": "9805"},
            "quantity": 9000,
        }
        mapped = schema.map_offer(offer_item, parent_product_id="169")
        self.assertEqual(mapped["parent_link"]["parent_product_id"], "169")
        self.assertTrue(mapped["parent_link"]["matches_queried_parent"])

    def test_mismatched_parent_is_detected_not_silently_accepted(self):
        offer_item = {"id": 9002, "parentId": 999, "quantity": 0}
        mapped = schema.map_offer(offer_item, parent_product_id="477")
        self.assertFalse(mapped["parent_link"]["matches_queried_parent"])


class PriceTypePreservationTests(unittest.TestCase):
    def test_regional_prices_are_never_collapsed_into_one_value(self):
        rows = [
            {"id": 1, "productId": 477, "catalogGroupId": 1, "price": "192000.0000", "currency": "RUB"},
            {"id": 2, "productId": 477, "catalogGroupId": 2, "price": "194000.0000", "currency": "RUB"},
            {"id": 3, "productId": 477, "catalogGroupId": 3, "price": "192000.0000", "currency": "RUB"},
        ]
        mapped = schema.map_prices(rows, price_type_names={1: "RETAIL", 2: "MSC", 3: "EKB"})
        self.assertEqual(len(mapped), 3)
        by_name = {p["price_type_name"]: p["amount"] for p in mapped}
        self.assertEqual(by_name, {"RETAIL": "192000.0000", "MSC": "194000.0000", "EKB": "192000.0000"})


class SeoEffectiveStatusTests(unittest.TestCase):
    def test_explicit_seo_reported_when_present(self):
        status = schema.seo_effective_status({"metaTitle": "T", "metaDescription": "D"})
        self.assertEqual(status["explicit_seo_title"], "T")
        self.assertEqual(status["explicit_seo_description"], "D")

    def test_effective_inherited_seo_is_never_fabricated(self):
        status = schema.seo_effective_status({})
        self.assertFalse(status["effective_seo_available"])
        self.assertIn("does not expose", status["effective_seo_unavailable_reason"])


class LiveAdapterNewOperationsTests(unittest.TestCase):
    def setUp(self):
        self._env = _EnvWebhook()
        self._env.__enter__()

    def tearDown(self):
        self._env.__exit__()

    def _adapter(self, **overrides) -> LiveBitrixAdapter:
        return LiveBitrixAdapter(config=_live_config(**overrides))

    def test_product_lookup_filters_by_id_and_iblock(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"products": [{"id": 477, "iblockId": 14, "name": "Телевизор Rews-788"}]}})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)
        out = adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "product_lookup", "bitrix_id": "477"})

        self.assertTrue(captured["url"].endswith("catalog.product.list.json"))
        self.assertEqual(captured["body"]["filter"], {"iblockId": 14, "id": 477})
        self.assertEqual(out["items"][0]["id"], 477)

    def test_product_lookup_requires_valid_id(self):
        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        with self.assertRaises(BitrixValidationError):
            adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "product_lookup"})

    def test_section_read_uses_catalog_section_list(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"sections": [{"id": 73, "name": "Curved TVs", "iblockSectionId": 70}]}})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)
        out = adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "section_read"})

        self.assertTrue(captured["url"].endswith("catalog.section.list.json"))
        self.assertEqual(captured["body"]["filter"], {"iblockId": 14})
        self.assertEqual(out["items"][0]["id"], 73)

    def test_offer_read_filters_by_offers_iblock_and_parent_id(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"offers": [{"id": 9001, "parentId": 477}]}})

        adapter = self._adapter(BITRIX_CATALOG_ID="14", BITRIX_OFFERS_IBLOCK_ID="15")
        adapter.client._http._client = _mock_transport(handler)
        out = adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "offer_read", "parent_product_id": "477"})

        self.assertTrue(captured["url"].endswith("catalog.product.offer.list.json"))
        self.assertEqual(captured["body"]["filter"], {"iblockId": 15, "parentId": 477})
        self.assertEqual(out["items"][0]["parentId"], 477)

    def test_offer_read_fails_closed_without_offers_iblock_id_and_makes_no_call(self):
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={"result": {"offers": []}})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")  # no BITRIX_OFFERS_IBLOCK_ID
        adapter.client._http._client = _mock_transport(handler)
        with self.assertRaises(IntegrationNotConfiguredError):
            adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "offer_read", "parent_product_id": "477"})
        self.assertEqual(called["n"], 0)

    def test_price_read_uses_catalog_price_list_filtered_by_product_id(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"prices": [{"id": 1, "productId": 477, "catalogGroupId": 1, "price": "192000", "currency": "RUB"}]}})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)
        out = adapter.read(capability="cms.bitrix.catalog.read", params={"operation": "price_read", "bitrix_id": "477"})

        self.assertTrue(captured["url"].endswith("catalog.price.list.json"))
        self.assertEqual(captured["body"]["filter"], {"productId": 477})
        self.assertEqual(out["items"][0]["catalogGroupId"], 1)

    def test_default_catalog_listing_behavior_unchanged(self):
        """Preservation: the already-production-verified default (no
        operation) catalog listing path must remain exactly as before."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": {"products": [{"id": 113, "iblockId": 14, "name": "Городской велосипед XR10"}]}})

        adapter = self._adapter(BITRIX_CATALOG_ID="14")
        adapter.client._http._client = _mock_transport(handler)
        out = adapter.read(capability="cms.bitrix.catalog.read", params={})

        self.assertTrue(captured["url"].endswith("catalog.product.list.json"))
        self.assertEqual(captured["body"], {"filter": {"iblockId": 14}, "select": ["id", "iblockId", "name"]})
        self.assertEqual(out["items"][0]["name"], "Городской велосипед XR10")


class _StubActivation:
    """Minimal stand-in for IntegrationActivationService.execute_via_gateway,
    routing by ``payload['operation']`` -- avoids re-mocking HTTP/adapter
    construction just to test BitrixProductBridge's orchestration."""

    def __init__(self, responses: dict, *, raise_on: dict | None = None):
        self._responses = responses
        self._raise_on = raise_on or {}

    def execute_via_gateway(self, *, payload, **kwargs):
        op = payload.get("operation")
        if op in self._raise_on:
            raise self._raise_on[op]
        return {"result": {"items": self._responses.get(op, [])}}


class VerifySchemaBindingTests(unittest.TestCase):
    def test_full_bounded_verification_report_for_known_product_477(self):
        activation = _StubActivation(
            {
                "product_lookup": [
                    {
                        "id": 477,
                        "iblockId": 14,
                        "name": "Телевизор Rews-788",
                        "iblockSectionId": 73,
                        "quantity": 1000,
                        "property100": {"value": "75", "valueId": "1"},
                        "property106": "banner-content",
                    }
                ],
                "section_read": [
                    {"id": 61, "name": "Электроника", "iblockSectionId": None},
                    {"id": 70, "name": "Телевизоры", "iblockSectionId": 61},
                    {"id": 73, "name": "Изогнутые телевизоры", "iblockSectionId": 70},
                ],
                "offer_read": [{"id": 9001, "parentId": 477, "quantity": 1000}],
                "price_read": [
                    {"id": 1, "productId": 477, "catalogGroupId": 1, "price": "192000", "currency": "RUB"},
                    {"id": 2, "productId": 477, "catalogGroupId": 2, "price": "194000", "currency": "RUB"},
                ],
            }
        )
        bridge = BitrixProductBridge(integration_activation=activation, environment="LIVE")
        report = bridge.verify_schema_binding(tenant_id="tenant-a", bitrix_product_id="477")

        self.assertTrue(report["found"])
        self.assertEqual(report["identity"]["id"], 477)
        self.assertEqual([a["id"] for a in report["category"]["ancestors"]], [61, 70, 73])
        self.assertEqual(report["brand"]["ownership"], schema.PANDA_MANAGED)
        self.assertEqual(len(report["prices"]), 2)
        self.assertEqual(report["offers"][0]["parent_link"]["parent_product_id"], 477)
        self.assertFalse(report["seo"]["effective_seo_available"])
        self.assertTrue(any("does not expose" in lim for lim in report["limitations"]))
        self.assertTrue(any("warehouse_stock_unavailable" in lim for lim in report["limitations"]))

    def test_product_not_found_reported_not_fabricated(self):
        activation = _StubActivation({"product_lookup": []})
        bridge = BitrixProductBridge(integration_activation=activation, environment="LIVE")
        report = bridge.verify_schema_binding(tenant_id="tenant-a", bitrix_product_id="999999")
        self.assertFalse(report["found"])
        self.assertIn("product_not_found", report["limitations"])

    def test_missing_offers_iblock_config_is_a_reported_limitation_not_a_crash(self):
        activation = _StubActivation(
            {
                "product_lookup": [{"id": 477, "iblockId": 14, "name": "X", "iblockSectionId": None, "quantity": 1000}],
                "section_read": [],
                "price_read": [],
            },
            raise_on={"offer_read": IntegrationNotConfiguredError("bitrix_offers_iblock_id_not_configured")},
        )
        bridge = BitrixProductBridge(integration_activation=activation, environment="LIVE")
        report = bridge.verify_schema_binding(tenant_id="tenant-a", bitrix_product_id="477")
        self.assertTrue(report["found"])
        self.assertIn("offers_iblock_id_not_configured", report["limitations"])
        self.assertEqual(report["offers"], [])


if __name__ == "__main__":
    unittest.main()
