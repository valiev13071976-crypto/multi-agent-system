"""TV product-write-contract defect closure — dedicated focused tests.

Bounded scope (per the task): fixes the Bitrix/Aspro product-write
contract so a non-variant TV Panda writes matches the verified Aspro
product structure, starting with real products 989/990 ("Телевизор LG
100MRGB96B6.ARUG"). No broad audit, no new architecture, no Telegram/
Managed Agent/spreadsheet-ingestion/pricing-formula changes; every write
below stays a mocked HTTP transport -- zero real Bitrix mutations.

ROOT CAUSE (proved by the tests below, not asserted on faith):
Panda's ``LiveBitrixAdapter._write_product_create_live`` created a real
Bitrix offer/SKU (IBLOCK 15) whenever ``sku`` had a value -- which every
product, variant or not, always has -- conflating "has an article/SKU
value" with "genuinely has variant dimensions" (color/size options across
multiple SKUs of the SAME item, which this single-product write path has
never actually carried). That is exactly why real product 989 was created
as a SKU-parent ("товар с предложениями") with a separate offer 990,
splitting catalog data across two Bitrix elements, unlike the verified
reference Aspro TV card ("Телевизор Folket HF-42") -- a normal, non-variant
catalog product with its own "Торговый каталог" tab directly on the
element, never a "Предложения" tab.

FIX: ``SingleProductWriteRequest`` gains ``has_variant_offer`` (default
``False`` -- every existing caller leaves it at its default, since this
write path has never carried real variant data). A real offer/SKU (IBLOCK
15) is now only ever created when a caller explicitly supplies
``has_variant_offer=True``. Every current single-product write -- TVs
included -- therefore becomes a plain, non-variant IBLOCK 14 product,
matching the verified reference; ARTICLE (property 283) and MORE_PHOTO
(property 280, gallery) are verified ONLY on the offers IBLOCK on this
installation, so they are correctly reported sourced-but-unwritten for
this default model, never guessed onto an unverified IBLOCK 14 property.

Test groups below (mirrors the task's own "TESTS" list):
  1. ProductContractModelTests       -- product model/contract
  2. ElementIdentityFieldMappingTests -- element/identity field mapping
  3. TvCharacteristicsMappingTests    -- TV characteristics mapping
  4. ImageAndGalleryMappingTests      -- gallery/main-image mapping
  5. PriceOwnershipTests              -- purchase + BASE retail price ownership
  6. SeoMappingTests                  -- SEO mapping (no verified destination)
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from business_assistant.controlled_bitrix_write import (
    PRODUCT_MODEL_SIMPLE,
    PRODUCT_MODEL_SKU_OFFER,
    STATUS_WRITE_VERIFIED,
    SingleProductWriteRequest,
    execute_single_product_write,
    prepare_single_product_write,
)
from integrations.bitrix import schema
from integrations.production.http import BoundedHttpClient
from tests.test_bitrix_live_product_create_write import _bridge_and_activation, _LiveEnv, _RecordingTransport

TARGET_TENANT = "tenant-real-customer"
# The exact real product this defect closure is about.
TARGET_TITLE = "Телевизор LG 100MRGB96B6.ARUG"
TARGET_SKU = "100MRGB96B6.ARUG"
TARGET_EAN = "8806096824788"
TARGET_BRAND = "LG"
TARGET_RETAIL_PRICE = "899990"
TARGET_PURCHASE_PRICE = "699990"
TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}

PREVIEW_PICTURE = {"filename": "preview.jpg", "base64": "cHJldmlldy1ieXRlcw=="}
DETAIL_PICTURE = {"filename": "detail.jpg", "base64": "ZGV0YWlsLWJ5dGVz"}
GALLERY_PICTURES = (
    {"filename": "gallery-1.jpg", "base64": "Z2FsbGVyeS1vbmU="},
    {"filename": "gallery-2.jpg", "base64": "Z2FsbGVyeS10d28="},
)

# Canonical TV characteristics from the 2026-09-23 LIVE audit of the
# CURRENT IBLOCK 14 + Aspro storefront + pre-Panda reference televisions
# (Mirt 174, Makaria 194). Historical form_element_14 aliases are not
# evidence: production proved many of them point to unrelated properties.
ALL_REQUESTED_TV_CHARACTERISTICS = {
    "weight_kg": "13.6",  # verified -> property147 / "Вес, кг"
    "screen_diagonal_cm": "124",  # verified -> property154
    "screen_resolution": "1920x1080",  # verified -> property156, pixels only
    "operating_system": "Android",  # verified -> property206
    "smart_tv_support": "Да",  # verified -> property209, stored as "Есть"
    "wifi_support": "Да",  # verified -> property210, stored as "Встроенный"
    "country_of_origin": "Китай",  # verified -> property174
    "refresh_rate_hz": "120 Гц",  # UNKNOWN destination: must NOT be written
    "panel_technology": "Mini LED",  # UNKNOWN destination
    "bluetooth_support": "Да",  # UNKNOWN destination
    "hdmi_count": "4",  # UNKNOWN destination
    "audio_power_w": "40 Вт",  # UNKNOWN destination
    "speaker_count": "2",  # UNKNOWN destination
    "vesa_mount": "300x300 мм",  # UNKNOWN destination
}
VERIFIED_TV_CHARACTERISTIC_KEYS = {
    "weight_kg",
    "screen_diagonal_cm",
    "screen_resolution",
    "operating_system",
    "smart_tv_support",
    "wifi_support",
    "country_of_origin",
}


def _tv_request(**overrides) -> SingleProductWriteRequest:
    base = dict(
        tenant_id=TARGET_TENANT,
        title=TARGET_TITLE,
        sku=TARGET_SKU,
        retail_price=TARGET_RETAIL_PRICE,
        ean=TARGET_EAN,
        brand=TARGET_BRAND,
        purchase_price=TARGET_PURCHASE_PRICE,
        subcategory="Телевизоры",
        short_description="Телевизор LG с диагональю 100 дюймов.",
        detailed_description="Полное описание телевизора LG 100MRGB96B6.ARUG.",
        characteristics=dict(ALL_REQUESTED_TV_CHARACTERISTICS),
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


def _preview(request: SingleProductWriteRequest, *, sections=None):
    transport = _RecordingTransport(sections=list(sections) if sections is not None else [TV_SECTION])
    with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
        bridge, _activation = _bridge_and_activation()
        preview = prepare_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request)
    return preview, transport


class ProductContractModelTests(unittest.TestCase):
    """1. TV product contract/model test.

    Proves the root-cause fix directly: a non-variant TV (this write
    path's only real use case, and the exact real 989/990 scenario)
    defaults to the SIMPLE product model -- no offer/SKU (IBLOCK 15) is
    ever created -- while a genuinely-variant product (an explicit
    ``has_variant_offer=True``) still gets the pre-existing offer
    mechanism, unchanged.
    """

    def test_non_variant_tv_defaults_to_simple_product_no_offer_created(self):
        preview, _transport = _preview(_tv_request())
        self.assertEqual(preview["status"], "REQUIRES_APPROVAL")
        self.assertEqual(preview["product_model"]["model"], PRODUCT_MODEL_SIMPLE)
        self.assertFalse(preview["product_model"]["has_variant_offer"])
        self.assertTrue(preview["product_model"]["reason"])

        result, transport = _execute(_tv_request())
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(transport.product_add_count, 1)
        self.assertEqual(transport.offer_add_count, 0)
        methods_called = [m for m, _ in transport.calls]
        self.assertNotIn("catalog.product.offer.add", methods_called)
        self.assertNotIn("catalog.product.offer.list", methods_called)

    def test_explicit_variant_offer_still_creates_the_offer_element(self):
        """The pre-existing mechanism is gated, not removed -- a caller
        that genuinely has a variant dimension still gets a real offer."""
        result, transport = _execute(_tv_request(has_variant_offer=True))
        self.assertEqual(result["status"], STATUS_WRITE_VERIFIED)
        self.assertEqual(result["product_model"]["model"], PRODUCT_MODEL_SKU_OFFER)
        self.assertEqual(transport.offer_add_count, 1)

    def test_default_has_variant_offer_is_false_on_every_new_request(self):
        """Every existing caller (row-based, field-based, enrichment
        pipeline) that does not explicitly pass ``has_variant_offer``
        must get the SIMPLE product model -- never a silent default to
        the offer/SKU model."""
        request = SingleProductWriteRequest(
            tenant_id=TARGET_TENANT, title=TARGET_TITLE, sku=TARGET_SKU, retail_price=TARGET_RETAIL_PRICE
        )
        self.assertFalse(request.has_variant_offer)


class ElementIdentityFieldMappingTests(unittest.TestCase):
    """2. TV field-mapping test (element/identity fields).

    Proves the exact resolved destination -- or explicit absence of one --
    for each element/identity field the task's TARGET TV CARD CONTRACT
    lists: name, article/SKU, brand, EAN, category/section.
    """

    def test_name_and_brand_land_on_the_base_product(self):
        result, transport = _execute(_tv_request())
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"]["name"], TARGET_TITLE)
        brand_property = schema.catalog_property(code="BRAND")
        self.assertEqual(product_body["fields"][brand_property.select_key], TARGET_BRAND)
        self.assertEqual(result["brand"], TARGET_BRAND)

    def test_article_sku_writes_to_the_base_product_cml2_article_property(self):
        """SIMPLE_PRODUCT TV contract-alignment pass (ticket T-F79758
        follow-up, production reference element 992/IBLOCK 14): article/
        SKU now has a verified BASE-PRODUCT destination -- CML2_ARTICLE/
        property 241 -- and is written there for the default, non-variant
        model. It must never land on the offer-only ARTICLE/283 property
        (that remains correct ONLY for a genuine has_variant_offer=True
        write), and must no longer be reported as unwritten."""
        result, transport = _execute(_tv_request())
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        article_property = schema.offer_property(code="ARTICLE")
        simple_article_property = schema.catalog_property(code="CML2_ARTICLE")
        self.assertEqual(simple_article_property.property_id, 241)
        self.assertNotIn(article_property.select_key, product_body["fields"])
        self.assertEqual(product_body["fields"][simple_article_property.select_key], TARGET_SKU)
        unwritten = {item["field"] for item in result["not_written"]}
        self.assertNotIn("sku", unwritten)
        self.assertEqual(result["sku"], TARGET_SKU)

    def test_ean_has_no_verified_destination_on_this_installation(self):
        result, transport = _execute(_tv_request())
        unwritten = {item["field"] for item in result["not_written"]}
        self.assertIn("ean", unwritten)
        self.assertNotIn(TARGET_EAN, json.dumps(transport.calls))

    def test_category_resolves_the_real_tv_section_dynamically_not_hardcoded(self):
        """The category resolver must choose the actual TV section from
        the real taxonomy snapshot -- never a hardcoded id, and never a
        specific child section like FULL HD when the parent TV section
        itself is the exact name match."""
        full_hd_child = {"id": 71, "name": "Full HD телевизоры", "iblockSectionId": TV_SECTION_ID}
        result, transport = _execute(_tv_request(), sections=[TV_SECTION, full_hd_child])
        self.assertEqual(result["resolved_section_id"], TV_SECTION_ID)
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"][schema.SECTION_FIELD], TV_SECTION_ID)


class TvCharacteristicsMappingTests(unittest.TestCase):
    """3. TV characteristics mapping test.

    Category-specific: only the characteristics with BOTH reliable Panda
    data AND a verified Bitrix property destination on THIS installation
    (schema.CATALOG_CHARACTERISTICS) are ever written; every other
    requested TV characteristic name (wifi, HDR, backlight type, screen
    format, refresh rate, sound power, speaker count, smart-home
    ecosystem, warranty, etc.) has no verified property here and must
    never be guessed onto one.
    """

    def test_only_the_verified_characteristics_reach_real_properties(self):
        result, transport = _execute(_tv_request())
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        fields = product_body["fields"]

        expected_wire_values = {
            **ALL_REQUESTED_TV_CHARACTERISTICS,
            "smart_tv_support": "Есть",
            "wifi_support": "Встроенный",
        }
        for key in VERIFIED_TV_CHARACTERISTIC_KEYS:
            binding = schema.characteristic_binding(key)
            self.assertIsNotNone(binding, f"{key} should be verified")
            self.assertEqual(fields[f"property{binding.property_id}"], expected_wire_values[key])
        self.assertEqual(set(result["characteristics_written"]), VERIFIED_TV_CHARACTERISTIC_KEYS)

    def test_unverified_tv_characteristics_are_reported_unmapped_never_guessed(self):
        result, transport = _execute(_tv_request())
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        fields = product_body["fields"]

        unmapped_keys = set(ALL_REQUESTED_TV_CHARACTERISTICS) - VERIFIED_TV_CHARACTERISTIC_KEYS
        reported_unmapped = {
            item["field"][len("characteristic:"):]
            for item in result["not_written"]
            if item["field"].startswith("characteristic:")
        }
        self.assertEqual(reported_unmapped, unmapped_keys)

        # The written property set is EXACTLY the verified characteristic
        # bindings -- no additional propertyN key exists for any unmapped
        # characteristic (``reported_unmapped == unmapped_keys`` above
        # already proves each one was correctly refused a destination;
        # this cross-checks it at the wire-field level too). Excludes the
        # non-characteristic base-product property fields this same call
        # also carries: BRAND (100), and -- since the SIMPLE_PRODUCT TV
        # contract-alignment pass -- article/CML2_ARTICLE (241) and
        # gallery/MORE_PHOTO (124), neither of which is a characteristic.
        verified_property_keys = {
            f"property{schema.characteristic_binding(key).property_id}" for key in VERIFIED_TV_CHARACTERISTIC_KEYS
        }
        non_characteristic_property_keys = {
            schema.catalog_property(code="BRAND").select_key,
            schema.catalog_property(code="CML2_ARTICLE").select_key,
            schema.catalog_property(code="MORE_PHOTO").select_key,
        }
        written_property_keys = {k for k in fields if k.startswith("property")} - non_characteristic_property_keys
        self.assertEqual(written_property_keys, verified_property_keys)

    def test_characteristics_are_written_regardless_of_product_model(self):
        """Characteristics live on the base product (IBLOCK 14) either
        way -- the product-model closure only affects article/gallery,
        which are offer-only properties."""
        simple_result, _ = _execute(_tv_request())
        variant_result, _ = _execute(_tv_request(has_variant_offer=True))
        self.assertEqual(set(simple_result["characteristics_written"]), VERIFIED_TV_CHARACTERISTIC_KEYS)
        self.assertEqual(set(variant_result["characteristics_written"]), VERIFIED_TV_CHARACTERISTIC_KEYS)


class RepresentativeTvCharacteristicMappingTests(unittest.TestCase):
    """Canonical LIVE TV map regression.

    The current site audit is authoritative. In particular property147 is
    "Вес, кг"; refresh rate has no verified destination and must never be
    sent to property147 or any other guessed property.
    """

    REPRESENTATIVE_MAPPING = {
        "weight_kg": 147,
        "screen_diagonal_cm": 154,
        "screen_resolution": 156,
        "country_of_origin": 174,
        "operating_system": 206,
        "smart_tv_support": 209,
        "wifi_support": 210,
    }

    def test_schema_bindings_match_current_live_property_ids(self):
        for key, expected_property_id in self.REPRESENTATIVE_MAPPING.items():
            binding = schema.characteristic_binding(key)
            self.assertIsNotNone(binding, f"{key} should be verified")
            self.assertEqual(binding.property_id, expected_property_id, key)

    def test_historical_cross_category_aliases_are_not_writable(self):
        for key in (
            "refresh_rate_hz",
            "panel_technology",
            "backlight_type",
            "speaker_count",
            "hdmi_version",
            "wireless_interfaces",
            "smart_tv_platform",
            "dimensions_with_stand",
            "weight_with_stand",
            "dimensions_without_stand",
            "weight_without_stand",
            "service_life",
        ):
            self.assertIsNone(schema.characteristic_binding(key), key)

    def test_refresh_rate_can_never_land_in_weight_property(self):
        values = {
            "weight_kg": "13.6",
            "refresh_rate_hz": "144 Гц",
        }
        result, transport = _execute(_tv_request(characteristics=values))
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        fields = product_body["fields"]

        self.assertEqual(fields["property147"], "13.6")
        self.assertNotIn("144", json.dumps(fields))
        self.assertEqual(set(result["characteristics_written"]), {"weight_kg"})
        self.assertIn(
            "characteristic:refresh_rate_hz",
            {item["field"] for item in result["not_written"]},
        )

    def test_site_string_vocab_is_normalized_for_smart_tv_and_wifi(self):
        values = {"smart_tv_support": "Yes", "wifi_support": "Yes"}
        result, transport = _execute(_tv_request(characteristics=values))
        fields = next(b for m, b in transport.calls if m == "catalog.product.add")["fields"]

        self.assertEqual(fields["property209"], "Есть")
        self.assertEqual(fields["property210"], "Встроенный")
        self.assertEqual(set(result["characteristics_written"]), set(values))


class ImageAndGalleryMappingTests(unittest.TestCase):
    """4. Gallery/main-image mapping test.

    PREVIEW_PICTURE/DETAIL_PICTURE (announcement image + detail image) are
    native IBLOCK 14 fields and always land on the base product,
    regardless of product model. SIMPLE_PRODUCT TV contract-alignment
    pass (ticket T-F79758 follow-up, production reference element 992/
    IBLOCK 14): gallery (MORE_PHOTO) now ALSO has a verified BASE-PRODUCT
    destination -- property 124 -- used for the default, non-variant TV
    model. The pre-existing offers-IBLOCK (15) MORE_PHOTO/280 destination
    is reused unchanged when a caller genuinely has a variant offer; the
    two are mutually exclusive per write.
    """

    def test_preview_and_detail_images_land_on_the_base_product(self):
        result, transport = _execute(_tv_request())
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        fields = product_body["fields"]
        self.assertEqual(
            fields[schema.PREVIEW_PICTURE_FIELD],
            {schema.PICTURE_FILE_DATA_KEY: [PREVIEW_PICTURE["filename"], PREVIEW_PICTURE["base64"]]},
        )
        self.assertEqual(
            fields[schema.DETAIL_PICTURE_FIELD],
            {schema.PICTURE_FILE_DATA_KEY: [DETAIL_PICTURE["filename"], DETAIL_PICTURE["base64"]]},
        )
        self.assertIn(schema.PREVIEW_PICTURE_FIELD, result["media_written"])
        self.assertIn(schema.DETAIL_PICTURE_FIELD, result["media_written"])

    def test_simple_product_writes_gallery_to_the_base_product_more_photo_property(self):
        """SIMPLE_PRODUCT gallery: gallery images -> PROPERTY_124, never
        suppressed as offer-only, never PROPERTY_280."""
        result, transport = _execute(_tv_request())
        self.assertEqual(result["gallery_written"], len(GALLERY_PICTURES))
        methods_called = [m for m, _ in transport.calls]
        self.assertNotIn("catalog.product.offer.add", methods_called)

        unwritten = {item["field"] for item in result["not_written"]}
        self.assertNotIn("gallery_pictures", unwritten)

        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        simple_more_photo = schema.catalog_property(code="MORE_PHOTO")
        offer_more_photo = schema.offer_property(code="MORE_PHOTO")
        self.assertEqual(simple_more_photo.property_id, 124)
        self.assertEqual(offer_more_photo.property_id, 280)
        self.assertNotIn(offer_more_photo.select_key, product_body["fields"])
        self.assertEqual(
            product_body["fields"][simple_more_photo.select_key],
            [
                {"value": {schema.PICTURE_FILE_DATA_KEY: [pic["filename"], pic["base64"]]}}
                for pic in GALLERY_PICTURES
            ],
        )

    def test_explicit_variant_offer_still_writes_gallery_to_the_verified_more_photo_property(self):
        result, transport = _execute(_tv_request(has_variant_offer=True))
        self.assertEqual(result["gallery_written"], len(GALLERY_PICTURES))
        offer_body = next(b for m, b in transport.calls if m == "catalog.product.offer.add")
        more_photo = schema.offer_property(code="MORE_PHOTO")
        self.assertEqual(
            offer_body["fields"][more_photo.select_key],
            [
                {"value": {schema.PICTURE_FILE_DATA_KEY: [pic["filename"], pic["base64"]]}}
                for pic in GALLERY_PICTURES
            ],
        )


class PriceOwnershipTests(unittest.TestCase):
    """5. Purchase + BASE retail price ownership test.

    Purchasing price/currency are native ``catalog.product`` fields, sent
    on the SAME base-product create call regardless of product model.
    The BASE retail price (BITRIX_RETAIL_PRICE_TYPE_ID) is a
    ``catalog.price.add`` row keyed to whichever entity is actually
    sellable -- for the SIMPLE product model that is the base product
    itself (no offer exists to attach it to); regional price types
    (OPT/MSC/EKB/MAGNITOGORSK) are never populated.
    """

    def test_purchase_price_and_currency_on_the_base_product(self):
        result, transport = _execute(_tv_request())
        self.assertTrue(result["purchase_price_written"])
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        self.assertEqual(product_body["fields"]["purchasingPrice"], TARGET_PURCHASE_PRICE)
        self.assertEqual(product_body["fields"]["purchasingCurrency"], "RUB")

    def test_base_retail_price_is_attached_to_the_base_product_id_for_the_simple_model(self):
        """For a SIMPLE product (no offer exists), the BASE retail price
        must be attached to the base product's own id -- there is no
        other sellable entity to attach it to."""
        result, transport = _execute(_tv_request())
        price_body = next(b for m, b in transport.calls if m == "catalog.price.add")
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        created_product_id = next(
            iter(v for k, v in transport._products_by_xml_id.items())  # noqa: SLF001
        )["id"]
        self.assertEqual(price_body["fields"]["productId"], created_product_id)
        self.assertEqual(price_body["fields"]["price"], TARGET_RETAIL_PRICE)
        self.assertEqual(str(result["bitrix_product_id"]), str(created_product_id))
        self.assertNotIn(schema.SECTION_FIELD, price_body["fields"])
        self.assertNotEqual(product_body, {})  # sanity: base product was actually created

    def test_no_regional_price_types_are_ever_populated(self):
        result, transport = _execute(_tv_request())
        price_calls = [b for m, b in transport.calls if m == "catalog.price.add"]
        # Exactly one price row (the configured BASE retail price type) --
        # never a second call for OPT/MSC/EKB/MAGNITOGORSK-style regional
        # price types, which this write path has no business writing.
        self.assertEqual(len(price_calls), 1)

    def test_purchase_price_never_substitutes_for_or_appears_in_the_retail_price_call(self):
        result, transport = _execute(_tv_request())
        price_body = next(b for m, b in transport.calls if m == "catalog.price.add")
        self.assertEqual(price_body["fields"]["price"], TARGET_RETAIL_PRICE)
        self.assertNotIn("purchasingPrice", price_body["fields"])
        self.assertNotIn(TARGET_PURCHASE_PRICE, json.dumps(price_body))

    def test_no_stock_or_min_max_price_is_ever_fabricated(self):
        """Stock stays deferred; min/max prices are Bitrix-derived rollups
        (schema.CATALOG_PRODUCT_PROPERTIES: MINIMUM_PRICE/MAXIMUM_PRICE
        are DERIVED, never written directly) -- this write never sends a
        quantity or a min/max price field."""
        result, transport = _execute(_tv_request())
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        for forbidden_key in ("quantity", "property97", "property98"):
            self.assertNotIn(forbidden_key, product_body["fields"])


class SeoMappingTests(unittest.TestCase):
    """6. SEO mapping test.

    This installation's real ``catalog.product.list``/
    ``catalog.product.offer.list`` responses carry no seo/meta/title/
    description/keyword-shaped field at all (schema.py module docstring,
    item G) -- there is currently no verified writable REST destination
    for SEO title/description/keywords, explicit or inherited. This write
    path must never invent one; SEO must never appear in any outbound
    Bitrix payload.
    """

    def test_no_seo_field_is_ever_sent_on_the_base_product(self):
        result, transport = _execute(_tv_request())
        product_body = next(b for m, b in transport.calls if m == "catalog.product.add")
        fields = product_body["fields"]
        for seo_key in (
            "metaTitle",
            "seoTitle",
            "metaDescription",
            "seoDescription",
            "metaKeywords",
            "seoKeywords",
            "elementMetaTitle",
            "elementMetaDescription",
        ):
            self.assertNotIn(seo_key, fields)

    def test_seo_effective_status_reports_the_exact_unavailable_reason_not_a_guess(self):
        status = schema.seo_effective_status({})
        self.assertFalse(status["effective_seo_available"])
        self.assertEqual(status["effective_seo_unavailable_reason"], schema.SEO_INHERITED_UNAVAILABLE_REASON)
        self.assertIsNone(status["explicit_seo_title"])
        self.assertIsNone(status["explicit_seo_description"])


if __name__ == "__main__":
    unittest.main()
