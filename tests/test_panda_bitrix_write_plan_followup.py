"""Production defect closure: the read-only "what exactly would be written
to Bitrix?" follow-up on an ALREADY prepared complete product card.

Exact reproduced two-turn case:

    turn 1: "Подготовь полную карточку товара ..."  (CALL_PRODUCT_ENRICHMENT,
            unchanged) -> complete enrichment preview, zero Bitrix mutation
    turn 2: "Покажи точно, какие данные из этой карточки товара будут
            записаны в Bitrix/Aspro, если я подтвержу запись ... Ничего в
            Bitrix не записывай."

Before the fix, turn 2 was routed to the generic business-workflow path,
whose reply merely echoed the instruction text back ("Requested: GENERATE:
Покажи точно, какие данные ... | Status: COMPLETED | Findings: 0 ..."). It
must instead answer from the enrichment state turn 1 already persisted,
through the EXISTING read-only ``prepare_single_product_write`` preview,
with ZERO Bitrix mutation.

Uses the FIXTURE Bitrix adapter and fake search/fetch/image ports only --
never a live network call, never a live mutation.
"""

from __future__ import annotations

import io
import unittest

import httpx

from business_assistant.action_continuation import EXPLAIN_BITRIX_WRITE_PLAN
from business_assistant.conversation_gateway import ConversationRequest
from business_assistant.intent import is_conversational
from product_enrichment.media_fetch import FakeImageFetcher
from tests.test_panda_product_enrichment_conversational import (
    ENRICHMENT_TURN_TEXT,
    PREVIEW_TURN_TEXT,
    TARGET_BRAND,
    TARGET_SKU,
    _bitrix_bridge,
    _panda,
    _price_list_bytes,
    _register_upload,
)
from tools.search.fake_provider import FakeSearchProvider, fake_result

RESEARCH_URL = "https://www.lg.com/ru/tv/55mrgb86b6a"
IMAGE_URL = "https://www.lg.com/ru/photos/55mrgb86b6a-hero.png"

WRITE_PLAN_FOLLOW_UP_TEXT = (
    "Покажи точно, какие данные из этой карточки товара будут записаны в Bitrix/Aspro, "
    "если я подтвержу запись. Покажи, какие характеристики будут записаны, статус каждой "
    "характеристики (verified/probable), какие характеристики НЕ будут записаны и почему, "
    "какие изображения будут записаны и какое описание будет записано. "
    "Ничего в Bitrix не записывай."
)

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


class WritePlanFollowUpAnswersFromPreparedCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_follow_up_returns_write_preview_instead_of_echoing_the_instruction(self):
        # The follow-up must reach the conversational gateway at all -- it
        # mentions "Bitrix" + a write verb, which previously classified it
        # as a generic business-integration request.
        self.assertTrue(is_conversational(WRITE_PLAN_FOLLOW_UP_TEXT))

        bridge, store = _bitrix_bridge()
        panda, artifact_service = _panda(
            bitrix_bridge=bridge,
            search_provider=FakeSearchProvider(
                {f"{TARGET_BRAND} {TARGET_SKU}": [fake_result(RESEARCH_URL, title=f"LG {TARGET_SKU}")]}
            ),
            scrape_fetch_handler=_scrape_fetch_handler,
            media_fetcher=FakeImageFetcher({IMAGE_URL: _png_bytes()}),
        )
        ref = await _register_upload(
            artifact_service, tenant="tenant-a", owner="u1", conv="c1", filename="LG.xlsx", content=_price_list_bytes()
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
        enrichment = await panda.respond(
            ConversationRequest(
                text=ENRICHMENT_TURN_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r2",
                conversation_id="c1",
            )
        )
        enrichment_preview = enrichment.metadata.get("enrichment_preview") or {}
        self.assertGreater((enrichment_preview.get("characteristics") or {}).get("count") or 0, 0)
        before = len(store.catalog("tenant-a"))

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
        # Zero Bitrix mutation from the read-only explanation.
        self.assertEqual(len(store.catalog("tenant-a")), before)
        self.assertFalse(answer.metadata.get("mutated"))

        text = answer.text
        # Never an echo of the instruction text.
        self.assertNotIn("Requested:", text)
        self.assertNotIn("Ничего в Bitrix не записывай", text)
        self.assertNotIn("статус каждой характеристики", text)

        # Actual product identity from the prepared card.
        self.assertIn(TARGET_SKU, text)
        self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ", text)
        self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ НЕ БУДУТ ЗАПИСАНЫ", text)

        # Actual characteristics, with their already-computed statuses.
        task = panda._action_store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")  # noqa: SLF001
        write_request = dict(task.parameters["bitrix_enrichment_write_request"])
        status_map = dict(task.parameters["bitrix_enrichment_characteristic_status"])
        self.assertTrue(status_map)
        for key in write_request.get("characteristics") or {}:
            self.assertIn(key, text)
        for key, info in status_map.items():
            self.assertIn(key, text)
            self.assertIn(str(info.get("confidence")), text)
        self.assertTrue(any(str(i.get("confidence")) == "verified" for i in status_map.values()))
        # A characteristic without a verified Bitrix property is reported as
        # excluded, with the reason -- never silently dropped.
        excluded = [key for key in status_map if key not in (write_request.get("characteristics") or {})]
        self.assertTrue(excluded)
        for key in excluded:
            self.assertIn(f"{key}: {status_map[key].get('value')}", text)
        self.assertIn("нет проверенного свойства в Bitrix", text)

        # What the EXISTING write path would include/exclude (read-only
        # prepare_single_product_write output, rendered verbatim).
        write_preview = answer.metadata.get("bitrix_write_preview") or {}
        self.assertEqual(write_preview.get("status"), "REQUIRES_APPROVAL")
        self.assertIn("Поля записи (по текущей политике записи):", text)
        for item in write_preview.get("will_write") or []:
            self.assertIn(str(item), text)
        for item in write_preview.get("will_not_write") or []:
            self.assertIn(str(item.get("reason")), text)

        # Prepared media (uploaded file names, never an external hotlink)
        # and the prepared description.
        self.assertIn("ИЗОБРАЖЕНИЯ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ", text)
        self.assertIn(str((write_request.get("preview_picture") or {}).get("filename")), text)
        self.assertNotIn(IMAGE_URL, text)
        self.assertIn("ОПИСАНИЕ, КОТОРОЕ БУДЕТ ЗАПИСАНО", text)
        self.assertIn(str(write_request.get("short_description")), text)
        self.assertIn(str(write_request.get("detailed_description")).splitlines()[0], text)


if __name__ == "__main__":
    unittest.main()
