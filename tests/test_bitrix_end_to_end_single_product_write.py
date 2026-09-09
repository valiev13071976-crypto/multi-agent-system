"""ONE complete end-to-end product write path, and the production blocker
that stopped it: ``no_matching_section_found``.

Scenario (the exact production sequence, through the existing production
code path -- no new writer, no new agent):

    XLSX upload + one-turn enrichment (search/fetch/media all faked, zero
    network) -> prepared card persisted on the active task -> explicit
    write confirmation -> the EXISTING governed single-product Bitrix write
    (LiveBitrixAdapter over a mocked HTTP transport with production
    response shapes).

Blocker: ``schema.resolve_section_id`` only accepted an EXACT section-name
match, so a supplier price list's own category vocabulary ("TV") could
never match the installation's Russian section names ("Телевизоры") and
every product failed closed after a successful HTTP 200
``catalog.section.list``.

ZERO live Bitrix mutations: every Bitrix REST call below is answered by the
in-process ``_LiveShapedTransport``; no real network call is made.
"""

from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

import httpx

from business_assistant.action_continuation import CALL_CONTROLLED_BITRIX_WRITE, CALL_PRODUCT_ENRICHMENT
from business_assistant.conversation_gateway import ConversationRequest, WorkflowPandaConversationGateway
from integrations.activation.models import ENV_LIVE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix import schema
from integrations.bitrix.catalog import BitrixCatalogStore
from integrations.bitrix.product_bridge import BitrixProductBridge
from integrations.production.http import BoundedHttpClient
from tests.test_panda_product_enrichment_conversational import (
    ENRICHMENT_TURN_TEXT,
    PREVIEW_TURN_TEXT,
    TARGET_BRAND,
    TARGET_CATEGORY,
    TARGET_EAN,
    TARGET_PURCHASE_PRICE,
    TARGET_SKU,
    USER_RETAIL_PRICE_RUB,
    _panda,
    _price_list_bytes,
    _register_upload,
)
from tools.search.fake_provider import FakeSearchProvider, fake_result

CONFIRMATION_TEXT = "Подтверждаю. Выполни реальную запись этого подготовленного товара в Bitrix/Aspro."

RESEARCH_URL = "https://www.lg.com/ru/tv/55mrgb86b6a"
MAIN_IMAGE_URL = "https://www.lg.com/ru/photos/55mrgb86b6a-main.png"
GALLERY_IMAGE_URLS = (
    "https://www.lg.com/ru/photos/55mrgb86b6a-side.png",
    "https://www.lg.com/ru/photos/55mrgb86b6a-back.png",
)

PRODUCT_PAGE_HTML = f"""<html><head>
<meta property="og:image" content="{MAIN_IMAGE_URL}">
</head><body>
<h1>LG {TARGET_SKU}</h1>
<table class="specs">
  <tr><td>Диагональ экрана</td><td>55"</td></tr>
  <tr><td>Частота обновления</td><td>120 Гц</td></tr>
  <tr><td>Цвет</td><td>черный</td></tr>
</table>
<img src="{GALLERY_IMAGE_URLS[0]}" alt="LG side">
<img src="{GALLERY_IMAGE_URLS[1]}" alt="LG back">
</body></html>"""

# Production-shaped catalog.section.list result: the shop's own Russian
# section names, nested through iblockSectionId. Nothing here is keyed to
# this brand/SKU/EAN, and no section id is hardcoded in production code.
LIVE_SECTIONS = [
    {"id": 101, "name": "Электроника", "sort": 100, "iblockSectionId": None},
    {"id": 102, "name": "Телевизоры и видео", "sort": 200, "iblockSectionId": 101},
    {"id": 103, "name": "Телевизоры", "sort": 300, "iblockSectionId": 102},
    {"id": 104, "name": "Смартфоны", "sort": 400, "iblockSectionId": 101},
]

CREATED_PRODUCT_ID = 7701
CREATED_OFFER_ID = 9901
RETAIL_PRICE_TYPE_ID = 7


def _png_bytes(shade: int) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (800, 800), (shade, 40, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scrape_fetch_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == RESEARCH_URL:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=PRODUCT_PAGE_HTML)
    return httpx.Response(404)


class _LiveShapedTransport:
    """Bitrix REST responses in the real production shapes, kept in memory."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._products: dict[str, dict] = {}
        self._offers: dict[int, dict] = {}
        self._prices: set[tuple[int, int]] = set()

    def payload_for(self, rest_method: str) -> dict:
        for name, body in self.calls:
            if name == rest_method:
                return dict(body.get("fields") or {})
        return {}

    def count(self, rest_method: str) -> int:
        return sum(1 for name, _ in self.calls if name == rest_method)

    def __call__(self, method: str, url: str, **kwargs) -> httpx.Response:
        import json

        body = json.loads(json.dumps(kwargs.get("json_body") or {}))
        rest = url.rsplit("/", 1)[-1].removesuffix(".json")
        self.calls.append((rest, body))
        filt = body.get("filter") or {}

        if rest == "catalog.section.list":
            return httpx.Response(200, json={"result": {"sections": LIVE_SECTIONS}})

        if rest == "catalog.product.list":
            if "xmlId" in filt:
                match = self._products.get(filt["xmlId"])
                return httpx.Response(200, json={"result": {"products": [match] if match else []}})
            if filt.get("id") is not None:
                match = next((p for p in self._products.values() if p["id"] == filt["id"]), None)
                return httpx.Response(200, json={"result": {"products": [match] if match else []}})
            return httpx.Response(200, json={"result": {"products": list(self._products.values())}})

        if rest == "catalog.product.add":
            fields = body["fields"]
            record = {
                "id": CREATED_PRODUCT_ID,
                "iblockId": 14,
                "name": fields["name"],
                "active": fields["active"],
                "property100": fields.get("property100"),
            }
            if fields.get(schema.SECTION_FIELD) is not None:
                record[schema.SECTION_FIELD] = fields[schema.SECTION_FIELD]
            self._products[fields["xmlId"]] = record
            return httpx.Response(
                200,
                json={"result": {"element": {"id": CREATED_PRODUCT_ID, "name": record["name"], "active": record["active"]}}},
            )

        if rest == "catalog.product.offer.list":
            parent = filt.get(schema.CML2_LINK_REST_FIELD)
            match = self._offers.get(parent)
            return httpx.Response(200, json={"result": {"offers": [match] if match else []}})

        if rest == "catalog.product.offer.add":
            parent = body["fields"]["parentId"]
            self._offers[parent] = {"id": CREATED_OFFER_ID, schema.CML2_LINK_REST_FIELD: parent}
            return httpx.Response(200, json={"result": {"offer": {"id": CREATED_OFFER_ID, "parentId": parent}}})

        if rest == "catalog.price.list":
            key = (filt.get("productId"), filt.get("catalogGroupId"))
            rows = [{"id": 1, "productId": key[0], "catalogGroupId": key[1]}] if key in self._prices else []
            return httpx.Response(200, json={"result": {"prices": rows}})

        if rest == "catalog.price.add":
            self._prices.add((body["fields"]["productId"], body["fields"]["catalogGroupId"]))
            return httpx.Response(200, json={"result": {"price": {"id": 1, "productId": body["fields"]["productId"]}}})

        raise AssertionError(f"unexpected Bitrix REST call: {rest}")


class _LiveEnv:
    KEYS = (
        "BITRIX_INTEGRATION_MODE",
        "BITRIX_WEBHOOK_URL",
        "BITRIX_CATALOG_ID",
        "BITRIX_OFFERS_IBLOCK_ID",
        "BITRIX_RETAIL_PRICE_TYPE_ID",
    )

    def __enter__(self):
        self._prior = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["BITRIX_INTEGRATION_MODE"] = "LIVE"
        os.environ["BITRIX_WEBHOOK_URL"] = "https://panda.example.invalid/rest/1/never-a-real-secret/"
        os.environ["BITRIX_CATALOG_ID"] = "14"
        os.environ["BITRIX_OFFERS_IBLOCK_ID"] = "15"
        os.environ["BITRIX_RETAIL_PRICE_TYPE_ID"] = str(RETAIL_PRICE_TYPE_ID)
        return self

    def __exit__(self, *exc):
        for key, value in self._prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class SupplierCategorySectionResolutionTests(unittest.TestCase):
    """The blocker itself, at the exact function that failed closed."""

    def test_supplier_category_vocabulary_resolves_to_the_real_shop_section(self):
        resolved = schema.resolve_section_id(category=TARGET_CATEGORY, sections=LIVE_SECTIONS)
        self.assertEqual(resolved["section_id"], 103)
        self.assertEqual(resolved["name"], "Телевизоры")
        self.assertEqual(resolved["match_kind"], "category_concept")

    def test_singular_supplier_value_resolves_to_the_plural_section_name(self):
        resolved = schema.resolve_section_id(subcategory="Телевизор", sections=LIVE_SECTIONS)
        self.assertEqual(resolved["section_id"], 103)
        self.assertEqual(resolved["match_kind"], "normalized_name")

    def test_exact_name_still_wins_unchanged(self):
        resolved = schema.resolve_section_id(subcategory="Смартфоны", sections=LIVE_SECTIONS)
        self.assertEqual(resolved["section_id"], 104)
        self.assertEqual(resolved["match_kind"], "exact_name")

    def test_broader_parent_section_is_never_used_as_a_fallback(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(
                category=TARGET_CATEGORY,
                sections=[{"id": 102, "name": "Телевизоры и видео"}],
            )
        self.assertEqual(ctx.exception.code, "no_matching_section_found")

    def test_two_sections_in_the_same_concept_fail_closed_as_ambiguous(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(
                category=TARGET_CATEGORY,
                sections=[{"id": 1, "name": "Телевизоры"}, {"id": 2, "name": "Телевизор"}],
            )
        self.assertEqual(ctx.exception.code, "ambiguous_section_name")

    def test_unknown_category_vocabulary_still_fails_closed(self):
        with self.assertRaises(schema.SectionResolutionError) as ctx:
            schema.resolve_section_id(category="CE", sections=LIVE_SECTIONS)
        self.assertEqual(ctx.exception.code, "no_matching_section_found")


class OneProductEndToEndGovernedWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_xlsx_to_enriched_card_to_confirmation_writes_one_complete_product(self):
        from product_enrichment.media_fetch import FakeImageFetcher

        transport = _LiveShapedTransport()
        search_provider = FakeSearchProvider(
            {f"{TARGET_BRAND} {TARGET_SKU}": [fake_result(RESEARCH_URL, title=f"LG {TARGET_SKU}")]}
        )
        media_fetcher = FakeImageFetcher(
            {
                MAIN_IMAGE_URL: _png_bytes(10),
                GALLERY_IMAGE_URLS[0]: _png_bytes(90),
                GALLERY_IMAGE_URLS[1]: _png_bytes(170),
            }
        )

        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=transport):
            bridge = BitrixProductBridge(
                integration_activation=IntegrationActivationService(),
                environment=ENV_LIVE,
                store=BitrixCatalogStore(),
            )
            panda, artifact_service = _panda(
                bitrix_bridge=bridge,
                search_provider=search_provider,
                scrape_fetch_handler=_scrape_fetch_handler,
                media_fetcher=media_fetcher,
            )
            self.assertIsInstance(panda, WorkflowPandaConversationGateway)
            ref = await _register_upload(
                artifact_service,
                tenant="tenant-a",
                owner="u1",
                conv="c1",
                filename="LG.xlsx",
                content=_price_list_bytes(),
            )
            await panda.respond(
                ConversationRequest(
                    text=PREVIEW_TURN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="r1",
                    conversation_id="c1",
                    attachment_refs=(ref,),
                )
            )
            card = await panda.respond(
                ConversationRequest(
                    text=ENRICHMENT_TURN_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="r2",
                    conversation_id="c1",
                )
            )
            searches_after_card = len(search_provider.queries)
            confirmation = await panda.respond(
                ConversationRequest(
                    text=CONFIRMATION_TEXT,
                    tenant_id="tenant-a",
                    user_id="u1",
                    request_id="r3",
                    conversation_id="c1",
                )
            )

        # The prepared card carried enrichment output into the write request.
        self.assertEqual(card.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        enrichment_preview = card.metadata.get("enrichment_preview") or {}
        self.assertGreater((enrichment_preview.get("characteristics") or {}).get("count") or 0, 0)
        self.assertTrue((enrichment_preview.get("media") or {}).get("main_image_prepared"))
        self.assertGreater((enrichment_preview.get("media") or {}).get("gallery_image_count") or 0, 0)

        # The confirmation executed the existing governed write exactly once.
        self.assertEqual(confirmation.metadata.get("action_decision"), CALL_CONTROLLED_BITRIX_WRITE)
        result = confirmation.metadata.get("bitrix_write_result") or {}
        self.assertEqual(result.get("status"), "WRITE_VERIFIED")
        self.assertTrue(result.get("mutated"))
        self.assertEqual(result.get("bitrix_product_id"), str(CREATED_PRODUCT_ID))
        self.assertEqual(transport.count("catalog.product.add"), 1)
        self.assertEqual(transport.count("catalog.product.offer.add"), 1)
        self.assertEqual(transport.count("catalog.price.add"), 1)
        # No enrichment/search re-run on the confirmation turn.
        self.assertEqual(len(search_provider.queries), searches_after_card)

        # --- the payload actually sent to Bitrix ---
        product_fields = transport.payload_for("catalog.product.add")
        # identity + resolved real catalog section
        self.assertEqual(product_fields["name"], f"LG {TARGET_SKU}")
        self.assertEqual(product_fields["property100"], TARGET_BRAND)
        self.assertEqual(product_fields[schema.SECTION_FIELD], 103)
        self.assertEqual(result.get("section_id_written"), 103)
        # purchase price -> native fields, never mixed into the retail price
        self.assertEqual(product_fields["purchasingPrice"], TARGET_PURCHASE_PRICE)
        self.assertEqual(product_fields["purchasingCurrency"], "RUB")
        # characteristics -> verified properties only
        self.assertTrue(result.get("characteristics_written"))
        for key in result["characteristics_written"]:
            binding = schema.characteristic_binding(key)
            self.assertIsNotNone(binding)
            self.assertIn(f"property{binding.property_id}", product_fields)
        # short + detailed description
        self.assertTrue(product_fields[schema.PREVIEW_TEXT_FIELD])
        self.assertTrue(product_fields[schema.DETAIL_TEXT_FIELD])
        # main image: uploaded bytes, never an external URL
        preview_picture = product_fields[schema.PREVIEW_PICTURE_FIELD]
        self.assertIn(schema.PICTURE_FILE_DATA_KEY, preview_picture)
        self.assertNotIn(MAIN_IMAGE_URL, str(preview_picture))
        self.assertIn(schema.DETAIL_PICTURE_FIELD, product_fields)
        # offer/SKU -> verified offer property, linked to the base product
        offer_fields = transport.payload_for("catalog.product.offer.add")
        self.assertEqual(offer_fields["parentId"], CREATED_PRODUCT_ID)
        article_binding = next(b for b in schema.OFFER_PROPERTIES if b.code == "ARTICLE")
        self.assertEqual(offer_fields[article_binding.select_key], TARGET_SKU)
        # retail price -> its own installation-specific price type
        price_fields = transport.payload_for("catalog.price.add")
        self.assertEqual(price_fields["price"], USER_RETAIL_PRICE_RUB)
        self.assertEqual(price_fields["catalogGroupId"], RETAIL_PRICE_TYPE_ID)
        self.assertEqual(price_fields["productId"], CREATED_PRODUCT_ID)

        # Fields with no verified destination on this installation are
        # reported, never guessed onto a property: EAN (no property in
        # either IBLOCK) stays sourced-but-unwritten.
        not_written_fields = {item.get("field") for item in result.get("not_written") or []}
        self.assertIn("ean", not_written_fields)
        self.assertNotIn(TARGET_EAN, str(product_fields))


if __name__ == "__main__":
    unittest.main()
