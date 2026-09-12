"""Production defect closure: the read-only "show me the FINAL plan for
this same product" follow-up, asked right after a one-turn XLSX-attached
enrichment request, degraded to the generic legacy business-workflow
recipe engine instead of reusing the already-prepared product/ActiveTask
state.

Exact reproduced production shape (two turns, one conversation):

    Turn 1 (XLSX attached to THIS turn): "Возьми первый товар из
        загруженного LG_TV.xlsx и подготовь полную карточку для
        Bitrix/Aspro. Используй все данные из строки товара, включая
        колонку «Цена». Покажи: название, артикул, EAN, бренд, закупочную
        цену, розничную цену, точный раздел Bitrix/Aspro с ID,
        характеристики, краткое и полное описание, SEO, основное
        изображение, галерею и полный план записи. Ничего не записывай и
        не публикуй в Bitrix без моего отдельного подтверждения."
        -> CALL_PRODUCT_ENRICHMENT (unchanged, already working).

    Turn 2 (NO new attachment -- attachment_count=0 is EXPECTED, the XLSX
        was already processed in Turn 1): "Покажи перед подтверждением
        записи окончательный план для этого же товара: закупочную цену,
        розничную цену, точный раздел Bitrix/Aspro с названием и ID, EAN,
        артикул, количество характеристик, основное изображение и
        количество изображений галереи. Ничего пока не записывай и не
        публикуй."

Before the fix, Turn 2 mentions "Bitrix"/"Aspro" plus an action verb
("покажи") but none of the existing routing predicates
(``is_bitrix_write_plan_question``, ``is_explicit_single_product_bitrix_
prep_request``, ``is_explicit_product_pricing_or_category_refinement_
request``) recognized this exact "окончательный план" phrasing (only the
narrower "будет записан"/"would be written" phrasing was recognized), so
``requires_business_integration`` matched it and ``is_conversational``
returned False. That sent Turn 2 to the attachment-blind LEGACY
``BusinessAssistantService.execute()`` recipe/plan engine instead of
``WorkflowPandaConversationGateway`` -- which never even looks at the
``ActiveTaskStore`` where Turn 1's prepared product/write-request state
already lives. The legacy engine's generic recipe then blocks several
steps on Bitrix capabilities this read-only question never needed
(``BA_CAPABILITY_UNAVAILABLE``/``dependency_not_ready``), and its reply is
composed from those blocked steps' empty findings -- the reported
"Задача выполнена. Подробности доступны в разделе управления." (rendered
by ``static/shared/presentation.js``'s ``opts.business`` branch whenever
the mapped status is COMPLETED with an empty/degraded summary).

The fix (this change set) is a single, additive regex extension in
``business_assistant.action_continuation._WRITE_PLAN_RE`` recognizing an
adjective ("окончательный"/"финальный"/"итоговый"/"final") immediately
before "план" as an ADDITIONAL alternative to the existing "будет
записан"/"would be written" phrase -- nothing else changed. Turn 2 now
reaches ``is_bitrix_write_plan_question`` -> True -> ``is_conversational``
-> True -> ``WorkflowPandaConversationGateway`` ->
``resolve_bitrix_write_plan_question`` -> the EXISTING, unchanged
``EXPLAIN_BITRIX_WRITE_PLAN`` handler, which answers from Turn 1's already
persisted ``bitrix_enrichment_write_request``/``bitrix_enrichment_preview``
state (never re-runs ingestion or enrichment, never writes to Bitrix).

Also covers the SAME Turn 2 follow-up sent a second time (production
reproduced the failure twice), and pins the exact routing predicates at
the unit level.
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import httpx
from openpyxl import Workbook

from business_assistant.action_continuation import (
    CALL_PRODUCT_ENRICHMENT,
    EXPLAIN_BITRIX_WRITE_PLAN,
    is_bitrix_write_plan_question,
    is_explicit_product_pricing_or_category_refinement_request,
    is_explicit_single_product_bitrix_prep_request,
)
from business_assistant.conversation_gateway import ConversationRequest
from business_assistant.intent import is_conversational, requires_business_integration
from product_enrichment.media_fetch import FakeImageFetcher
from tests.test_bitrix_live_product_create_write import _bridge_and_activation, _LiveEnv, _RecordingTransport
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload
from tools.search.fake_provider import FakeSearchProvider, fake_result

TARGET_SKU = "55MRGB86B6A.ARUG"
TARGET_PRODUCT_NAME = "LG 55MRGB86B6A.ARUG"
TARGET_BRAND = "LG"
TARGET_EAN = "8806096824788"
TARGET_PURCHASE_PRICE = "103198.3"
# A bare "Цена" (role ROLE_PRICE) column -- no separate "розница" column --
# exactly the shape PR #66 restored, and exactly the shape Turn 1 itself
# tells Panda to use ("включая колонку «Цена»").
TARGET_RETAIL_PRICE = "119990"

TV_SECTION_ID = 70
TV_SECTION_NAME = "Телевизоры"
TV_SECTION = {"id": TV_SECTION_ID, "name": TV_SECTION_NAME, "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# Turn 1: the EXACT reproduced production request -- ONE message, XLSX
# attached to THIS SAME turn, full-card enrichment requested in the same
# breath (mirrors the real production turn verbatim).
TURN1_TEXT = (
    "Возьми первый товар из загруженного LG_TV.xlsx и подготовь полную карточку для "
    "Bitrix/Aspro. Используй все данные из строки товара, включая колонку «Цена». "
    "Покажи: название, артикул, EAN, бренд, закупочную цену, розничную цену, точный "
    "раздел Bitrix/Aspro с ID, характеристики, краткое и полное описание, SEO, "
    "основное изображение, галерею и полный план записи. Ничего не записывай и не "
    "публикуй в Bitrix без моего отдельного подтверждения."
)

# Turn 2: the EXACT reproduced production follow-up that degraded to the
# generic legacy workflow -- SAME conversation, NO new attachment, refers
# back to "этого же товара" (the product Turn 1 already prepared).
TURN2_TEXT = (
    "Покажи перед подтверждением записи окончательный план для этого же товара: "
    "закупочную цену, розничную цену, точный раздел Bitrix/Aspro с названием и ID, "
    "EAN, артикул, количество характеристик, основное изображение и количество "
    "изображений галереи. Ничего пока не записывай и не публикуй."
)

RESEARCH_URL = "https://www.lg.com/ru/tv/55mrgb86b6a"
IMAGE_URL = "https://www.lg.com/ru/photos/55mrgb86b6a-hero.png"
GALLERY_IMAGE_URL = "https://www.lg.com/ru/photos/55mrgb86b6a-side.png"

PRODUCT_PAGE_HTML = f"""<html><head>
<meta property="og:image" content="{IMAGE_URL}">
</head><body>
<h1>LG {TARGET_SKU}</h1>
<img src="{GALLERY_IMAGE_URL}">
<table class="specs">
  <tr><td>Диагональ экрана</td><td>55"</td></tr>
  <tr><td>Частота обновления</td><td>120 Гц</td></tr>
  <tr><td>Цвет</td><td>черный</td></tr>
  <tr><td>Bluetooth</td><td>5.3</td></tr>
</table>
</body></html>"""

# Markers that only ever appear in the LEGACY fixture business-workflow's
# generic diagnostic summary / degraded-completion UI copy -- never in a
# conversational ConversationResult.text. Their presence anywhere below
# would mean this turn degraded to the generic workflow again (the exact
# reported production symptom).
WORKFLOW_DIAGNOSTIC_MARKERS = (
    "Задача выполнена",
    "Подробности доступны",
    "Requested:",
    "Findings:",
    "Fixture_mode:",
    "Waiting_approval:",
)


def _control_product_xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "цена"])
    ws.append(
        [
            TARGET_SKU,
            TARGET_PRODUCT_NAME,
            TV_SECTION_NAME,
            TARGET_BRAND,
            TARGET_EAN,
            TARGET_PURCHASE_PRICE,
            TARGET_RETAIL_PRICE,
        ]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _png_bytes(color=(12, 24, 36)) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (600, 600), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scrape_fetch_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == RESEARCH_URL:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=PRODUCT_PAGE_HTML)
    return httpx.Response(404)


class ProductPlanFollowUpRoutingUnitTests(unittest.TestCase):
    """Pins the exact routing predicates involved, at the unit level, so a
    future change to any of them is caught immediately -- independent of
    the full multi-turn integration test below."""

    def test_turn2_requires_business_integration_alone_would_misroute_it(self):
        # Documents WHY the defect happened: absent the fix below, nothing
        # else carves this phrasing out of the generic business-integration
        # path.
        self.assertTrue(requires_business_integration(TURN2_TEXT))
        self.assertFalse(is_explicit_single_product_bitrix_prep_request(TURN2_TEXT))
        self.assertFalse(is_explicit_product_pricing_or_category_refinement_request(TURN2_TEXT))

    def test_turn2_matches_write_plan_question_predicate(self):
        self.assertTrue(is_bitrix_write_plan_question(TURN2_TEXT))

    def test_turn2_routes_conversational_without_a_new_attachment(self):
        self.assertTrue(is_conversational(TURN2_TEXT, has_attachments=False))

    def test_turn1_is_unaffected_by_the_new_predicate(self):
        # Turn 1's own "...галерею и полный план записи." must keep routing
        # to CALL_PRODUCT_ENRICHMENT exactly as before -- it has no
        # "окончательный/финальный/итоговый план" adjective and is anyway
        # excluded via ``is_explicit_product_enrichment_request``.
        self.assertFalse(is_bitrix_write_plan_question(TURN1_TEXT))


class ProductPlanFollowUpContinuationDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        self.live_env = _LiveEnv()
        self.live_env.__enter__()
        self.http_patch = patch.object(
            __import__("integrations.production.http", fromlist=["BoundedHttpClient"]).BoundedHttpClient,
            "request",
            side_effect=self.transport,
        )
        self.http_patch.start()
        self.bridge, _activation = _bridge_and_activation()

    async def asyncTearDown(self):
        self.http_patch.stop()
        self.live_env.__exit__(None, None, None)

    async def test_final_plan_follow_up_reuses_prepared_product_and_is_asked_twice(self):
        panda, artifact_service = _panda(
            bitrix_bridge=self.bridge,
            search_provider=FakeSearchProvider(
                {f"{TARGET_BRAND} {TARGET_SKU}": [fake_result(RESEARCH_URL, title=f"LG {TARGET_SKU}")]}
            ),
            scrape_fetch_handler=_scrape_fetch_handler,
            media_fetcher=FakeImageFetcher(
                {IMAGE_URL: _png_bytes(), GALLERY_IMAGE_URL: _png_bytes((200, 40, 40))}
            ),
        )
        ref = await _register_upload(
            artifact_service,
            tenant="tenant-a",
            owner="u1",
            conv="c1",
            filename="LG_TV.xlsx",
            content=_control_product_xlsx_bytes(),
        )

        # Turn 1: XLSX attached to THIS turn, full-card enrichment requested
        # in the SAME message -- the exact one-turn production shape.
        turn1 = await panda.respond(
            ConversationRequest(
                text=TURN1_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(ref,),
            )
        )
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, turn1.text)
        self.assertEqual(turn1.metadata.get("action_decision"), CALL_PRODUCT_ENRICHMENT)
        self.assertFalse(turn1.metadata.get("mutated"))
        enrichment_preview = turn1.metadata.get("enrichment_preview") or {}
        self.assertGreater((enrichment_preview.get("characteristics") or {}).get("count") or 0, 0)
        self.assertEqual((enrichment_preview.get("media") or {}).get("status"), "ready")
        self.assertIn(TARGET_SKU, turn1.text)
        self.assertIn(TARGET_EAN, turn1.text)

        before_mutations = len(self.transport.calls)

        # Turn 2, asked TWICE (production reproduced the failure on both
        # the first AND the repeated request) -- NO new attachment.
        for turn_index, request_id in enumerate(("r2", "r3"), start=1):
            with self.subTest(attempt=turn_index):
                answer = await panda.respond(
                    ConversationRequest(
                        text=TURN2_TEXT,
                        tenant_id="tenant-a",
                        user_id="u1",
                        request_id=request_id,
                        conversation_id="c1",
                    )
                )
                text = answer.text

                # Never the generic degraded-workflow reply.
                for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
                    self.assertNotIn(marker, text)

                # The existing prepared product state was reused -- routed
                # through the EXISTING EXPLAIN_BITRIX_WRITE_PLAN handler,
                # never re-ingesting Excel or re-running enrichment.
                self.assertEqual(answer.metadata.get("action_decision"), EXPLAIN_BITRIX_WRITE_PLAN)
                self.assertFalse(answer.metadata.get("mutated"))

                # Identity: SKU/article, EAN, brand.
                self.assertIn(TARGET_SKU, text)
                self.assertIn(TARGET_EAN, text)
                self.assertIn(TARGET_BRAND, text)
                # Purchase price and retail price (PR #66's restored path,
                # untouched and still correct here).
                self.assertIn(TARGET_PURCHASE_PRICE, text)
                self.assertIn(TARGET_RETAIL_PRICE, text)
                # Exact Bitrix/Aspro section -- ID always, and the source
                # category name already carried on the prepared card (the
                # EXISTING write-plan renderer already includes it inside
                # the "resolved from ..." field-record, read verbatim, not
                # invented here).
                self.assertIn(str(TV_SECTION_ID), text)
                self.assertIn(TV_SECTION_NAME, text)
                # Characteristic count.
                self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ", text)
                write_request = dict(
                    answer.metadata.get("bitrix_write_preview", {}).get("target_product", {}) or {}
                )
                task = panda._action_store.get(  # noqa: SLF001
                    tenant_id="tenant-a", owner_id="u1", conversation_id="c1"
                )
                persisted_write_request = dict(task.parameters.get("bitrix_enrichment_write_request") or {})
                self.assertTrue(persisted_write_request.get("characteristics"))
                char_count = len(persisted_write_request["characteristics"])
                self.assertIn(f"ЗАПИСАНЫ: {char_count}", text)
                # Main image (uploaded filename, never a raw hotlink) and
                # gallery image count.
                preview_picture = dict(persisted_write_request.get("preview_picture") or {})
                self.assertTrue(preview_picture.get("filename"))
                self.assertIn(str(preview_picture.get("filename")), text)
                self.assertNotIn(IMAGE_URL, text)
                self.assertGreater((enrichment_preview.get("media") or {}).get("gallery_image_count") or 0, 0)
                self.assertIn("галерея:", text)
                # Explicit statement that nothing has been written yet.
                self.assertIn("сейчас ничего не записано", text.casefold())
                self.assertIn(
                    "Ничего в Bitrix не записано: это только предпросмотр.", text
                )

                write_preview = answer.metadata.get("bitrix_write_preview") or {}
                self.assertEqual(write_preview.get("status"), "REQUIRES_APPROVAL")

        # ZERO Bitrix mutation anywhere across all 3 turns -- the only
        # Bitrix call made anywhere is the read-only ``catalog.section.
        # list`` (the recording transport raises on any unexpected call,
        # including ``catalog.product.add``).
        methods_called = [m for m, _ in self.transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)
        self.assertGreaterEqual(len(self.transport.calls), before_mutations)


if __name__ == "__main__":
    unittest.main()
