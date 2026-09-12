"""ONE control LG product, from LG_TV.xlsx, through the COMPLETE existing
Panda -> Bitrix/Aspro flow, end to end (post retail-price restoration).

This is not a new pipeline and not a new test pattern: it is the SAME
three already-closed, already-tested steps chained together in ONE test,
the same way a real operator's conversation actually flows:

    1. supplier XLSX row preview (data_intel, unchanged Block 5.5) --
       "Найди товар ... и подготовь его для добавления в Bitrix/Aspro."
    2. "Подготовь полную карточку товара." (CALL_PRODUCT_ENRICHMENT,
       unchanged product_enrichment pipeline: identity/characteristics/
       content/media, via FakeSearchProvider + a mocked scrape.fetch
       transport + FakeImageFetcher -- exactly
       ``tests/test_panda_bitrix_write_plan_followup.py``'s own
       established, zero-real-network pattern).
    3. the read-only "what exactly would be written to Bitrix?" follow-up
       (EXPLAIN_BITRIX_WRITE_PLAN, unchanged), with the Bitrix bridge
       configured LIVE against a mocked HTTP transport (mirrors
       ``tests/test_bitrix_live_product_create_write.py``) so the REAL
       category resolver (``schema.resolve_section_id``) runs end to end.

The ONLY thing that changed for this test to succeed end-to-end is the
retail-price restoration in ``data_intel.service._row_lookup_result``
(this same change set): the control product's row carries a bare "цена"
column (role ROLE_PRICE) -- no "розница"/selling-price-specific column --
exactly the shape of the real reported production defect. Before that
fix, ``retail_price_preview`` stayed empty and the final write plan
degraded to ``missing_or_invalid_retail_price`` with no category shown
(see the accompanying diagnostic report). Nothing else in this chain was
touched -- enrichment, identity, characteristics, content, media, and the
Bitrix write/category machinery are all the pre-existing, unchanged
implementations.

Verifies the complete card contains every field the EXISTING pipeline
supports: product name, SKU/article, EAN, brand, purchase price, retail
price, the exact resolved Bitrix/Aspro category, characteristics (with
their verified/probable status), a short + detailed description, a main
(preview) image -- and confirms gallery images/SEO fields are not part of
the existing write-plan payload (tracked here as an explicit, honest
"not supported by the existing write path" fact, not silently assumed).

Zero Bitrix mutation anywhere in this test: the recording transport
raises on any unexpected call (including ``catalog.product.add``), and no
explicit write confirmation is ever sent.
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import httpx
from openpyxl import Workbook

from business_assistant.action_continuation import CALL_PRODUCT_ENRICHMENT, EXPLAIN_BITRIX_WRITE_PLAN
from business_assistant.conversation_gateway import ConversationRequest
from integrations.production.http import BoundedHttpClient
from product_enrichment.media_fetch import FakeImageFetcher
from tests.test_bitrix_live_product_create_write import _bridge_and_activation, _LiveEnv, _RecordingTransport
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload
from tools.search.fake_provider import FakeSearchProvider, fake_result

TARGET_SKU = "55MRGB86B6A.ARUG"
TARGET_PRODUCT_NAME = "LG 55MRGB86B6A.ARUG"
TARGET_BRAND = "LG"
TARGET_EAN = "8806096824788"
TARGET_PURCHASE_PRICE = "103198.3"
# The control product's ONLY price column in the source row -- a bare
# "цена" (role ROLE_PRICE), never a "розница"/selling-price-specific
# header. This is the exact shape that used to leave ``retail_price``
# empty before this change set's fix.
TARGET_GENERIC_PRICE = "119990"

TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

PREVIEW_TURN_TEXT = f"Найди товар LG {TARGET_SKU} и подготовь его для добавления в Bitrix/Aspro."
ENRICHMENT_TURN_TEXT = "Подготовь полную карточку товара."
WRITE_PLAN_FOLLOW_UP_TEXT = (
    "Покажи точно, какие данные из этой карточки товара будут записаны в Bitrix/Aspro, "
    "если я подтвержу запись. Покажи, какие характеристики будут записаны, статус каждой "
    "характеристики (verified/probable), какие характеристики НЕ будут записаны и почему, "
    "какие изображения будут записаны и какое описание будет записано. "
    "Ничего в Bitrix не записывай."
)

RESEARCH_URL = "https://www.lg.com/ru/tv/55mrgb86b6a"
IMAGE_URL = "https://www.lg.com/ru/photos/55mrgb86b6a-hero.png"

PRODUCT_PAGE_HTML = f"""<html><head>
<meta property="og:image" content="{IMAGE_URL}">
</head><body>
<h1>LG {TARGET_SKU}</h1>
<table class="specs">
  <tr><td>Диагональ экрана</td><td>55"</td></tr>
  <tr><td>Частота обновления</td><td>120 Гц</td></tr>
  <tr><td>Цвет</td><td>черный</td></tr>
  <tr><td>Bluetooth</td><td>5.3</td></tr>
</table>
</body></html>"""


def _control_product_xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "цена"])
    ws.append(
        [
            TARGET_SKU,
            TARGET_PRODUCT_NAME,
            "Телевизоры",
            TARGET_BRAND,
            TARGET_EAN,
            TARGET_PURCHASE_PRICE,
            TARGET_GENERIC_PRICE,
        ]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _png_bytes() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (600, 600), (12, 24, 36))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scrape_fetch_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == RESEARCH_URL:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=PRODUCT_PAGE_HTML)
    return httpx.Response(404)


class ControlProductEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        self.live_env = _LiveEnv()
        self.live_env.__enter__()
        self.http_patch = patch.object(BoundedHttpClient, "request", side_effect=self.transport)
        self.http_patch.start()
        self.bridge, _activation = _bridge_and_activation()

    async def asyncTearDown(self):
        self.http_patch.stop()
        self.live_env.__exit__(None, None, None)

    async def test_control_product_end_to_end_with_complete_card_and_write_plan(self):
        panda, artifact_service = _panda(
            bitrix_bridge=self.bridge,
            search_provider=FakeSearchProvider(
                {f"{TARGET_BRAND} {TARGET_SKU}": [fake_result(RESEARCH_URL, title=f"LG {TARGET_SKU}")]}
            ),
            scrape_fetch_handler=_scrape_fetch_handler,
            media_fetcher=FakeImageFetcher({IMAGE_URL: _png_bytes()}),
        )
        ref = await _register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_control_product_xlsx_bytes(),
        )

        # Step 1: row preview -- unchanged Block 5.5 XLSX ingestion.
        preview = await panda.respond(
            ConversationRequest(
                text=PREVIEW_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )
        self.assertIn(TARGET_SKU, preview.text)
        self.assertIn(TARGET_EAN, preview.text)
        # Retail price now correctly read from the row's bare "цена"
        # column -- the exact behavior this change set restores.
        self.assertIn(TARGET_GENERIC_PRICE, preview.text)

        # Step 2: complete-card enrichment -- unchanged product_enrichment
        # pipeline (identity/characteristics/content/media), zero Bitrix
        # mutation.
        before_mutations = len(self.transport.calls)
        enrichment = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )
        self.assertEqual(enrichment.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        enrichment_preview = enrichment.metadata.get("enrichment_preview") or {}
        self.assertGreater((enrichment_preview.get("characteristics") or {}).get("count") or 0, 0)
        self.assertEqual((enrichment_preview.get("media") or {}).get("status"), "ready")

        # Step 3: the read-only "what would be written" follow-up --
        # unchanged EXPLAIN_BITRIX_WRITE_PLAN handler, this time with a
        # LIVE-mode bridge so the REAL category resolver runs end to end.
        answer = await panda.respond(
            ConversationRequest(
                text=WRITE_PLAN_FOLLOW_UP_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r3",
                conversation_id="c1",
            )
        )
        self.assertEqual(answer.metadata.get("action_decision"), EXPLAIN_BITRIX_WRITE_PLAN)
        self.assertFalse(answer.metadata.get("mutated"))
        text = answer.text

        # The complete card -- every field the EXISTING pipeline supports.
        self.assertIn(TARGET_SKU, text)  # SKU/article
        self.assertIn(TARGET_EAN, text)  # EAN
        self.assertIn(TARGET_BRAND, text)  # brand
        self.assertIn(TARGET_PURCHASE_PRICE, text)  # purchase price
        self.assertIn(TARGET_GENERIC_PRICE, text)  # retail price (restored)
        self.assertIn(str(TV_SECTION_ID), text)  # exact resolved Bitrix/Aspro category

        write_preview = answer.metadata.get("bitrix_write_preview") or {}
        self.assertEqual(
            write_preview.get("status"),
            "REQUIRES_APPROVAL",
            "the full card must reach a real, resolvable write plan once retail price is known",
        )
        self.assertIn("Поля записи (по текущей политике записи):", text)

        # Characteristics, with their already-computed verified/probable
        # status (unchanged product_enrichment/characteristics.py).
        task = panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")  # noqa: SLF001
        write_request = dict(task.parameters["bitrix_enrichment_write_request"])
        status_map = dict(task.parameters["bitrix_enrichment_characteristic_status"])
        self.assertTrue(status_map)
        self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ", text)
        for key in write_request.get("characteristics") or {}:
            self.assertIn(key, text)

        # Short + detailed description (unchanged product_enrichment/content.py).
        self.assertIn("ОПИСАНИЕ, КОТОРОЕ БУДЕТ ЗАПИСАНО", text)
        self.assertTrue(write_request.get("short_description"))
        self.assertIn(str(write_request.get("short_description")), text)
        self.assertTrue(write_request.get("detailed_description"))

        # Main (preview) image -- uploaded bytes, never a raw hotlink.
        self.assertIn("ИЗОБРАЖЕНИЯ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ", text)
        preview_picture = write_request.get("preview_picture") or {}
        self.assertTrue(preview_picture.get("filename"))
        self.assertIn(str(preview_picture.get("filename")), text)
        self.assertNotIn(IMAGE_URL, text)

        # Honest, explicit facts about what the EXISTING write path does
        # NOT support yet (not silently assumed, not fixed here -- storefront
        # gaps are reviewed only after the live single-product publish):
        # SingleProductWriteRequest carries no gallery-image or SEO-field
        # destination at all.
        from business_assistant.controlled_bitrix_write import SingleProductWriteRequest

        write_fields = {f.name for f in SingleProductWriteRequest.__dataclass_fields__.values()}
        self.assertNotIn("gallery_images", write_fields)
        self.assertNotIn("seo_title", write_fields)
        self.assertNotIn("seo_description", write_fields)

        # ZERO Bitrix mutation across the whole 3-step flow -- the only
        # mocked Bitrix call made anywhere is the read-only
        # ``catalog.section.list`` (the recording transport would raise on
        # any unexpected call, including ``catalog.product.add``).
        methods_called = [m for m, _ in self.transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)
        self.assertGreaterEqual(len(self.transport.calls), before_mutations)


if __name__ == "__main__":
    unittest.main()
