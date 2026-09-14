"""Production defect closure — Panda -> Bitrix/Aspro product write mapping
against the REAL installed Aspro Premier product structure (real product
Bitrix ID 989, "Телевизор LG 100MRGB96B6.ARUG").

Confirmed production symptoms this closes:

  1. PRICE DISPLAY DEFECT: the real Bitrix admin form showed
     ``899990.00000000`` instead of the normal monetary value ``899990``.
     Root cause (proved below): ``business_assistant.controlled_bitrix_write
     ._normalize_price``/``data_intel.cleaning.normalize_decimal_string``
     only ever VALIDATE a price string, they never canonicalize its
     precision -- ``format(Decimal("899990.00000000"), "f")`` faithfully
     preserves however many (possibly spurious) trailing decimal digits
     the SOURCE text already carried, and that exact string was sent
     straight through to ``catalog.price.add``'s ``price`` field / the
     native ``purchasingPrice`` field, which Bitrix's admin form then
     echoes verbatim. Fixed with a new ``_normalize_money`` helper used
     ONLY for retail/purchase price (never for physical dimensions, which
     schema.py's own module docstring says must never be unit-converted):
     canonicalizes to standard 2-decimal (kopeck) monetary precision using
     the SAME ``ROUND_HALF_UP`` convention already established elsewhere
     in this codebase (``data_intel.economics.MONEY_SCALE``), then drops a
     trailing ``.00`` only when BOTH decimal digits are actually zero --
     the calculated numeric value itself is never changed, and no new
     pricing algorithm is introduced.

  2. GALLERY DEFECT: "Gallery/photogallery fields visible in Aspro are
     empty even though Panda prepared gallery images." Root cause (proved
     below): ``product_enrichment_bridge.build_enriched_write_request``
     never forwarded enrichment's gallery assets (``role == "gallery"``)
     into ``SingleProductWriteRequest`` at all -- the OLD
     ``format_write_plan_text`` line literally told the user "gallery —
     will NOT be written (no supported destination in this write path)".
     Fixed by adding ``SingleProductWriteRequest.gallery_pictures`` and
     wiring it to the verified OFFER-level MORE_PHOTO property (280,
     IBLOCK 15 -- ``integrations.bitrix.schema.OFFER_PROPERTIES``), using
     Bitrix's own documented multi-value FILE property write shape (an
     array of ``{"value": {"fileData": [name, base64]}}`` entries) --
     never the base product, and never a guessed shape.

Confirmed-CORRECT-AS-DESIGNED (no code change made, proved here so the
report does not have to take it on faith):

  - Base product vs. offer separation: ARTICLE/SKU (property 283) is
    installed ONLY on the OFFERS IBLOCK (15) on this installation --
    there is no verified base-product/IBLOCK 14 destination for it, so
    the base product's "Артикул" field staying empty is the CORRECT
    structural model (schema.py: "Do not blindly move a value merely
    because a field exists"), not a defect to invent a fix for.
  - Characteristics: only 5 canonical keys
    (screen_diagonal_cm/screen_resolution/operating_system/
    smart_tv_support/color) have a verified Bitrix property destination
    on this installation (``integrations.bitrix.schema
    .CATALOG_CHARACTERISTICS``); every other prepared characteristic is
    correctly left unmapped rather than guessed onto an arbitrary
    property.
  - EAN and SEO still have no verified Bitrix destination on this
    installation and are correctly never written.
  - Category/section resolution (id 70, "Телевизоры") is unchanged and
    persists correctly via the native ``iblockSectionId`` field.

Every Bitrix call in this file is a mocked HTTP transport (the same
``_RecordingTransport``/``_LiveEnv`` pattern already established by
``tests/test_bitrix_live_product_create_write.py``) -- zero real network
calls, zero real Bitrix mutations, and the real product 989 is never
referenced by ID or touched by any call here.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from business_assistant.controlled_bitrix_write import (
    STATUS_WRITE_VERIFIED,
    SingleProductWriteRequest,
    execute_single_product_write,
    prepare_single_product_write,
)
from integrations.bitrix import schema
from integrations.production.http import BoundedHttpClient
from tests.test_bitrix_live_product_create_write import (
    CREATED_PRODUCT_ID,
    _bridge_and_activation,
    _LiveEnv,
    _RecordingTransport,
)

TARGET_TENANT = "tenant-real-customer"
TARGET_TITLE = "Телевизор LG 100MRGB96B6.ARUG"
TARGET_SKU = "100MRGB96B6.ARUG"
TARGET_EAN = "8806096824788"
TARGET_BRAND = "LG"
TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}

# The exact reported defect shape -- excess decimal precision the SOURCE
# data carried, never introduced by this test.
RAW_RETAIL_PRICE = "899990.00000000"
RAW_PURCHASE_PRICE = "699990.00000000"
EXPECTED_RETAIL_PRICE_ON_WIRE = "899990"
EXPECTED_PURCHASE_PRICE_ON_WIRE = "699990"

PREVIEW_PICTURE = {"filename": "preview.jpg", "base64": "cHJldmlldy1ieXRlcw=="}
DETAIL_PICTURE = {"filename": "detail.jpg", "base64": "ZGV0YWlsLWJ5dGVz"}
GALLERY_PICTURES = (
    {"filename": "gallery-1.jpg", "base64": "Z2FsbGVyeS1vbmU="},
    {"filename": "gallery-2.jpg", "base64": "Z2FsbGVyeS10d28="},
)


def _control_product_request(**overrides) -> SingleProductWriteRequest:
    base = dict(
        tenant_id=TARGET_TENANT,
        title=TARGET_TITLE,
        sku=TARGET_SKU,
        retail_price=RAW_RETAIL_PRICE,
        ean=TARGET_EAN,
        brand=TARGET_BRAND,
        purchase_price=RAW_PURCHASE_PRICE,
        subcategory="Телевизоры",
        short_description="Телевизор LG с диагональю 100 дюймов.",
        detailed_description="Полное описание телевизора LG 100MRGB96B6.ARUG.",
        characteristics={
            # Verified -- must reach the wire as real propertyN fields.
            "screen_diagonal_cm": "254",
            "smart_tv_support": "Да",
            # NOT verified on this installation (schema.py) -- must be
            # reported sourced-but-unwritten, never guessed onto a
            # property.
            "refresh_rate_hz": "120",
            "panel_technology": "OLED",
        },
        preview_picture=PREVIEW_PICTURE,
        detail_picture=DETAIL_PICTURE,
        gallery_pictures=GALLERY_PICTURES,
    )
    base.update(overrides)
    return SingleProductWriteRequest(**base)


def _execute(request: SingleProductWriteRequest, *, sections=None):
    transport = _RecordingTransport(sections=list(sections) if sections is not None else [TV_SECTION])
    with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
        bridge, _activation = _bridge_and_activation()
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request, approved=True)
    return result, transport


class PriceDisplayDefectClosureTests(unittest.TestCase):
    """Item 8 / 10 of the report: proves the exact root cause and fix for
    ``899990.00000000`` -- and that the underlying CALCULATED value is
    never altered, only its wire representation."""

    def test_retail_price_reaches_catalog_price_add_without_excess_decimals(self):
        result, transport = _execute(_control_product_request())
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)

        price_calls = [b for m, b in transport.calls if m == "catalog.price.add"]
        self.assertEqual(len(price_calls), 1)
        self.assertEqual(price_calls[0]["fields"]["price"], EXPECTED_RETAIL_PRICE_ON_WIRE)
        # The exact defective string must never reach Bitrix.
        self.assertNotIn(RAW_RETAIL_PRICE, json.dumps(transport.calls))

    def test_purchase_price_reaches_native_field_without_excess_decimals(self):
        result, transport = _execute(_control_product_request())
        self.assertTrue(result["purchase_price_written"])

        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        self.assertEqual(product_calls[0]["fields"]["purchasingPrice"], EXPECTED_PURCHASE_PRICE_ON_WIRE)
        self.assertNotIn(RAW_PURCHASE_PRICE, json.dumps(transport.calls))

    def test_numeric_value_is_unchanged_not_recalculated(self):
        """899990.00000000 and 899990 are the exact same amount -- this is
        a representation fix, never a new price calculation."""
        from decimal import Decimal

        result, transport = _execute(_control_product_request())
        price_calls = [b for m, b in transport.calls if m == "catalog.price.add"]
        self.assertEqual(Decimal(price_calls[0]["fields"]["price"]), Decimal(RAW_RETAIL_PRICE))
        self.assertEqual(result["retail_price"]["amount"], EXPECTED_RETAIL_PRICE_ON_WIRE)

    def test_genuinely_fractional_price_keeps_its_two_decimals(self):
        """A real kopeck value (e.g. "22513.70") must never be truncated
        to "22513.7" -- only spurious EXCESS precision is removed."""
        result, transport = _execute(_control_product_request(purchase_price="22513.70", retail_price="29990"))
        self.assertTrue(result["purchase_price_written"])
        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        self.assertEqual(product_calls[0]["fields"]["purchasingPrice"], "22513.70")
        price_calls = [b for m, b in transport.calls if m == "catalog.price.add"]
        self.assertEqual(price_calls[0]["fields"]["price"], "29990")

    def test_normalize_money_unit_behavior(self):
        """Direct unit-level pin of the fix, independent of the full write
        flow."""
        from business_assistant.controlled_bitrix_write import _normalize_money  # noqa: SLF001

        self.assertEqual(_normalize_money("899990.00000000"), "899990")
        self.assertEqual(_normalize_money("29990"), "29990")
        self.assertEqual(_normalize_money("22513.70"), "22513.70")
        self.assertEqual(_normalize_money("0"), None)
        self.assertEqual(_normalize_money("not-a-number"), None)


class BaseProductVsOfferSeparationTests(unittest.TestCase):
    """Item 1/2 of the report: proves EXACTLY which fields land on the
    base product vs. the offer/SKU, per the installed schema -- never
    guessed, never blindly duplicated onto both."""

    def test_article_sku_only_on_offer_never_on_base_product(self):
        result, transport = _execute(_control_product_request())
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)

        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        offer_calls = [b for m, b in transport.calls if m == "catalog.product.offer.add"]
        self.assertEqual(len(product_calls), 1)
        self.assertEqual(len(offer_calls), 1)

        # ARTICLE (property 283) is verified ONLY on IBLOCK 15 (offers) --
        # the base product must never carry it under any key.
        article_property = schema.offer_property(code="ARTICLE")
        self.assertNotIn(article_property.select_key, product_calls[0]["fields"])
        self.assertEqual(offer_calls[0]["fields"][article_property.select_key], TARGET_SKU)

        # BRAND (property 100) is verified ONLY on IBLOCK 14 (base
        # product) -- the offer must never carry it.
        brand_property = schema.catalog_property(code="BRAND")
        self.assertEqual(product_calls[0]["fields"][brand_property.select_key], TARGET_BRAND)
        self.assertNotIn(brand_property.select_key, offer_calls[0]["fields"])

    def test_ean_is_never_written_to_either_entity(self):
        """No verified Bitrix destination exists for EAN on this
        installation -- it must never be guessed onto ANY property, base
        product or offer."""
        result, transport = _execute(_control_product_request())
        self.assertIn("ean", {item["field"] for item in result["not_written"]})
        self.assertNotIn(TARGET_EAN, json.dumps(transport.calls))


class CategoryPersistenceTests(unittest.TestCase):
    """Item 3 of the report: the resolved TV section (70) must persist on
    the base product's native ``iblockSectionId`` field, and only there."""

    def test_resolved_section_70_is_written_and_read_back(self):
        result, transport = _execute(_control_product_request())
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(result["resolved_section_id"], TV_SECTION_ID)

        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        self.assertEqual(product_calls[0]["fields"][schema.SECTION_FIELD], TV_SECTION_ID)

        offer_calls = [b for m, b in transport.calls if m == "catalog.product.offer.add"]
        self.assertNotIn(schema.SECTION_FIELD, offer_calls[0]["fields"])

        # Read-back verification (execute_single_product_write's own
        # independent governed read) must have confirmed it too.
        self.assertTrue(result["read_back"]["matches"])


class CharacteristicsMappingTests(unittest.TestCase):
    """Item 4 of the report: prepared characteristic -> resolved Bitrix
    property -> written value, and unmapped ones stay unmapped."""

    def test_verified_characteristics_map_to_real_properties_unverified_stay_unmapped(self):
        result, transport = _execute(_control_product_request())
        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        fields = product_calls[0]["fields"]

        diagonal = schema.characteristic_binding("screen_diagonal_cm")
        smart_tv = schema.characteristic_binding("smart_tv_support")
        self.assertEqual(fields[f"property{diagonal.property_id}"], "254")
        self.assertEqual(fields[f"property{smart_tv.property_id}"], "Да")

        self.assertEqual(sorted(result["characteristics_written"]), ["screen_diagonal_cm", "smart_tv_support"])

        unmapped_fields = {item["field"] for item in result["not_written"] if item["field"].startswith("characteristic:")}
        self.assertEqual(unmapped_fields, {"characteristic:refresh_rate_hz", "characteristic:panel_technology"})
        # Never guessed onto ANY property id.
        self.assertNotIn("120", json.dumps(fields))
        self.assertNotIn("OLED", json.dumps(fields))


class DescriptionMappingTests(unittest.TestCase):
    """Item 6 of the report: short -> previewText, full -> detailText."""

    def test_short_and_full_description_map_to_correct_native_fields(self):
        result, transport = _execute(_control_product_request())
        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        fields = product_calls[0]["fields"]
        self.assertEqual(fields[schema.PREVIEW_TEXT_FIELD], "Телевизор LG с диагональю 100 дюймов.")
        self.assertEqual(fields[schema.DETAIL_TEXT_FIELD], "Полное описание телевизора LG 100MRGB96B6.ARUG.")
        self.assertIn("media_written", result)


class MediaAndGalleryMappingTests(unittest.TestCase):
    """Item 5 of the report: preview/detail on the base product, gallery
    on the offer via the verified MORE_PHOTO property (280) using
    Bitrix's own documented multi-value FILE property write shape."""

    def test_preview_and_detail_pictures_on_base_product(self):
        result, transport = _execute(_control_product_request())
        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        fields = product_calls[0]["fields"]
        self.assertEqual(
            fields[schema.PREVIEW_PICTURE_FIELD],
            {schema.PICTURE_FILE_DATA_KEY: [PREVIEW_PICTURE["filename"], PREVIEW_PICTURE["base64"]]},
        )
        self.assertEqual(
            fields[schema.DETAIL_PICTURE_FIELD],
            {schema.PICTURE_FILE_DATA_KEY: [DETAIL_PICTURE["filename"], DETAIL_PICTURE["base64"]]},
        )

    def test_gallery_maps_to_offer_more_photo_never_base_product(self):
        result, transport = _execute(_control_product_request())
        self.assertEqual(result["gallery_written"], len(GALLERY_PICTURES))

        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        offer_calls = [b for m, b in transport.calls if m == "catalog.product.offer.add"]
        more_photo = schema.offer_property(code="MORE_PHOTO")

        self.assertNotIn(more_photo.select_key, product_calls[0]["fields"])
        gallery_fields = offer_calls[0]["fields"][more_photo.select_key]
        self.assertEqual(
            gallery_fields,
            [
                {"value": {schema.PICTURE_FILE_DATA_KEY: [pic["filename"], pic["base64"]]}}
                for pic in GALLERY_PICTURES
            ],
        )

    def test_no_gallery_supplied_means_no_more_photo_call_at_all(self):
        result, transport = _execute(_control_product_request(gallery_pictures=()))
        self.assertEqual(result["gallery_written"], 0)
        offer_calls = [b for m, b in transport.calls if m == "catalog.product.offer.add"]
        more_photo = schema.offer_property(code="MORE_PHOTO")
        self.assertNotIn(more_photo.select_key, offer_calls[0]["fields"])


class SeoAndUnrelatedFieldsTests(unittest.TestCase):
    """Items 7/9 of the report: SEO has no verified destination on this
    installation and is never written; no unrelated Aspro merchandising/
    banner/system field is ever populated just to "fill the form"."""

    def test_no_seo_field_is_ever_sent(self):
        result, transport = _execute(_control_product_request())
        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        fields = product_calls[0]["fields"]
        for seo_key in ("metaTitle", "seoTitle", "metaDescription", "seoDescription", "metaKeywords"):
            self.assertNotIn(seo_key, fields)

    def test_only_known_verified_field_names_are_ever_sent_on_base_product(self):
        result, transport = _execute(_control_product_request())
        product_calls = [b for m, b in transport.calls if m == "catalog.product.add"]
        fields = product_calls[0]["fields"]

        allowed_prefixes_or_names = {
            "iblockId",
            "name",
            "active",
            "xmlId",
            schema.catalog_property(code="BRAND").select_key,
            "purchasingPrice",
            "purchasingCurrency",
            schema.SECTION_FIELD,
            schema.PREVIEW_TEXT_FIELD,
            schema.PREVIEW_TEXT_TYPE_FIELD,
            schema.PREVIEW_PICTURE_FIELD,
            schema.DETAIL_TEXT_FIELD,
            schema.DETAIL_TEXT_TYPE_FIELD,
            schema.DETAIL_PICTURE_FIELD,
            f"property{schema.characteristic_binding('screen_diagonal_cm').property_id}",
            f"property{schema.characteristic_binding('smart_tv_support').property_id}",
        }
        self.assertEqual(set(fields.keys()), allowed_prefixes_or_names)
        # None of the ASPRO_MANAGED banner/button properties from
        # schema.CATALOG_PRODUCT_PROPERTIES are ever present.
        for binding in schema.CATALOG_PRODUCT_PROPERTIES:
            if binding.ownership == schema.ASPRO_MANAGED:
                self.assertNotIn(binding.select_key, fields)


class ZeroMutationAndIsolationTests(unittest.TestCase):
    """Items 12/13 of the report."""

    def test_prepare_single_product_write_never_mutates(self):
        transport = _RecordingTransport(sections=[TV_SECTION])
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge, _activation = _bridge_and_activation()
            preview = prepare_single_product_write(
                bridge, tenant_id=TARGET_TENANT, request=_control_product_request()
            )
        self.assertEqual(preview["status"], "REQUIRES_APPROVAL")
        self.assertEqual(transport.product_add_count, 0)
        self.assertEqual(transport.offer_add_count, 0)
        self.assertEqual(transport.price_add_count, 0)

    def test_no_market_intel_or_telegram_import_anywhere_in_this_module(self):
        import business_assistant.controlled_bitrix_write as module
        import inspect

        source = inspect.getsource(module)
        self.assertNotIn("market_intel", source)
        self.assertNotIn("telegram", source.lower())


if __name__ == "__main__":
    unittest.main()
