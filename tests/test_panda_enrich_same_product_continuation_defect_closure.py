"""Production defect closure: after PR #67 (routing) and PR #68 (row-preview
state bridge) shipped, an explicit ACTION request to enrich/prepare the
SAME already-selected product ("выполни полную подготовку / обогащение...")
is misrouted to the read-only ``EXPLAIN_BITRIX_WRITE_PLAN`` path instead of
invoking the existing ``CALL_PRODUCT_ENRICHMENT`` pipeline.

Exact reproduced production shape (SAME conversation as PR #68's
``b8f5ae10-5f3a-465e-ac93-58c90b93da27``):

    Turn 1 (XLSX attached to THIS turn): "Прикрепляю LG_TV.xlsx. Подготовь
        первый товар из файла для Bitrix/Aspro." -> the plain row-preview
        (NOT enrichment) -- PR #68's own reproduction, still asserted here.

    Turn 2 (no attachment): "Покажи ... окончательный план для этого же
        товара: ..." -> ``EXPLAIN_BITRIX_WRITE_PLAN`` reusing Turn 1's
        ``bitrix_product_fields`` -- PR #68's own fix, still asserted here.

    Turn 3 (no attachment): "Для этого же товара LG 100MRGB96B6.ARUG
        выполни полную подготовку карточки перед записью в Bitrix/Aspro.
        Используй уже загруженный LG_TV.xlsx и сохранённые данные этого
        товара, повторно файл не запрашивай. ... Выполни обогащение
        товара: характеристики, краткое и полное описание, SEO, основное
        изображение и галерею. Определи точный раздел Bitrix/Aspro. После
        подготовки покажи полный окончательный план: ... Ничего не
        записывай и не публикуй в Bitrix без моего отдельного
        подтверждения."

        Production symptom: Panda immediately re-rendered the existing
        NON-enriched ``EXPLAIN_BITRIX_WRITE_PLAN`` again (characteristics:
        0, images: none, descriptions: none, status UNRESOLVED) instead of
        running enrichment first.

DIAGNOSIS (proven by reproduction, not guessed):

1. ``resolve_action_turn`` already checks ``is_explicit_product_
   enrichment_request`` BEFORE ``is_bitrix_write_plan_question`` (an
   explicit "prepare the complete card" instruction is meant to always win
   -- see that function's own docstring/comments), and ``is_bitrix_write_
   plan_question`` itself already defers to ``is_explicit_product_
   enrichment_request`` when it matches. The PRECEDENCE ORDER was never
   the bug.

2. THE MISMATCH: ``is_explicit_product_enrichment_request`` requires an
   enrichment verb ("подготов"/"обогат"/...) AND the literal ADJACENT
   phrase "полную карточ"/"полная карточ"/"complete card" in the SAME
   message. Turn 3's phrasing is "выполни ПОЛНУЮ ПОДГОТОВКУ КАРТОЧКИ" --
   "полную" modifies "подготовку", not "карточки", so the two words are
   no longer adjacent and the existing stems never match, even though the
   enrichment verb ("подготов"/"обогат") IS present (twice: "подготовку"
   and "обогащение"). With ``is_explicit_product_enrichment_request``
   returning False, ``resolve_action_turn`` falls through to ``is_bitrix_
   write_plan_question``, which matches on "покажи ... окончательный
   план ... Bitrix/Aspro ..." (PR #67's own phrase) and returns the
   existing, NON-enriched preview -- the exact reported symptom.

3. The existing FAMILY_EXCEL/``bitrix_product_fields`` active-task state
   from PR #68 IS available and sufficient for ``resolve_product_
   enrichment_request`` to dispatch ``CALL_PRODUCT_ENRICHMENT`` without
   any new XLSX attachment or re-ingestion -- confirmed below.

FIX (this change set): ``is_explicit_product_enrichment_request`` gains an
alternative "target" signal alongside the existing literal "full card"
phrase: the SAME enrichment verb plus TWO OR MORE of the enrichment
pipeline's own component nouns (characteristics/description/SEO/image/
gallery) in the SAME message -- exactly what "Выполни обогащение товара:
характеристики, ... SEO, ... изображение и галерею." explicitly asks for.
A bare "подготовь"/"обогати" alone (no component nouns, no "full card"
phrase) still never matches. A plain read-only "покажи окончательный
план ... характеристик ..." (no enrichment VERB at all) still never
matches either -- PR #67's own write-plan question keeps routing to
``EXPLAIN_BITRIX_WRITE_PLAN`` unchanged (asserted below).
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import (
    FAMILY_EXCEL,
    is_bitrix_write_plan_question,
    is_explicit_product_enrichment_request,
)
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant_api.models import ST_COMPLETED
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from integrations.production.http import BoundedHttpClient
from tests.test_bitrix_live_product_create_write import _bridge_and_activation, _LiveEnv, _RecordingTransport
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

TARGET_SKU = "100MRGB96B6.ARUG"
TARGET_TITLE = "Телевизор LG 100MRGB96B6.ARUG"
TARGET_EAN = "8806096796849"
TARGET_BRAND = "LG"
TARGET_PURCHASE_PRICE = "717790.3"
TARGET_RETAIL_PRICE = "799990"
TARGET_CATEGORY_SOURCE = "Телевизоры"
FILENAME = "LG_TV.xlsx"

TV_SECTION_ID = 70
TV_SECTION_NAME = "Телевизоры"
TV_SECTION = {"id": TV_SECTION_ID, "name": TV_SECTION_NAME, "code": "televizory"}

# Turn 1: plain row-preview/single-product Bitrix-prep request (PR #68's
# own reproduction) -- XLSX attached to THIS turn.
TURN1_TEXT = "Прикрепляю LG_TV.xlsx. Подготовь первый товар из файла для Bitrix/Aspro."

# Turn 2: PR #67/#68's own "final plan" follow-up -- no attachment.
TURN2_TEXT = (
    "Покажи перед подтверждением записи окончательный план для этого же товара: "
    "закупочную цену, розничную цену, точный раздел Bitrix/Aspro с названием и ID, "
    "EAN, артикул, количество характеристик, основное изображение и количество "
    "изображений галереи. Ничего пока не записывай и не публикуй."
)

# Turn 3: the EXACT reproduced production defect -- explicit ENRICH ACTION
# for the SAME product, no attachment, ending with an embedded "show the
# final plan" ask that must NOT win over the action itself.
TURN3_TEXT = (
    "Для этого же товара LG 100MRGB96B6.ARUG выполни полную подготовку карточки перед "
    "записью в Bitrix/Aspro. Используй уже загруженный LG_TV.xlsx и сохранённые данные "
    "этого товара, повторно файл не запрашивай. Используй колонку «Цена» из файла как "
    "существующую розничную цену по текущей логике Panda. Выполни обогащение товара: "
    "характеристики, краткое и полное описание, SEO, основное изображение и галерею. "
    "Определи точный раздел Bitrix/Aspro. После подготовки покажи полный окончательный "
    "план: закупочная цена, розничная цена, раздел Bitrix с названием и ID, EAN, артикул, "
    "характеристики, описания, SEO, основное изображение и галерея. Ничего не записывай "
    "и не публикуй в Bitrix без моего отдельного подтверждения."
)

# The exact reported production symptom for Turn 3 -- the NON-enriched
# write-plan render must never reappear for this turn.
NON_ENRICHED_MARKER = "UNRESOLVED (missing_or_invalid_retail_price)"
WRITE_PLAN_HEADER = "ЧТО БУДЕТ ЗАПИСАНО В BITRIX/ASPRO ПРИ ПОДТВЕРЖДЕНИИ"
ENRICHMENT_PREVIEW_HEADER = "ПОЛНАЯ КАРТОЧКА ТОВАРА"

WORKFLOW_DIAGNOSTIC_MARKERS = ("Requested:", "Findings:", "Fixture_mode:", "Waiting_approval:")


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "цена"])
    ws.append(
        [
            TARGET_SKU,
            TARGET_TITLE,
            TARGET_CATEGORY_SOURCE,
            TARGET_BRAND,
            TARGET_EAN,
            TARGET_PURCHASE_PRICE,
            TARGET_RETAIL_PRICE,
        ]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class EnrichSameProductRoutingUnitTests(unittest.TestCase):
    """Pins the exact predicate-level mismatch and its fix in isolation."""

    def test_turn3_is_recognized_as_an_explicit_enrichment_request(self):
        self.assertTrue(is_explicit_product_enrichment_request(TURN3_TEXT))

    def test_turn3_write_plan_question_defers_to_the_enrichment_request(self):
        # is_bitrix_write_plan_question already refuses any text that
        # satisfies is_explicit_product_enrichment_request -- this pins
        # that the fix makes that guard actually fire for Turn 3.
        self.assertFalse(is_bitrix_write_plan_question(TURN3_TEXT))

    def test_bare_preparation_verb_alone_is_unaffected(self):
        self.assertFalse(is_explicit_product_enrichment_request("Подготовь товар."))
        self.assertFalse(is_explicit_product_enrichment_request("Обогати карточку."))

    def test_pr67_plain_write_plan_question_is_unaffected(self):
        # PR #67/#68's own "final plan" follow-up carries NO enrichment
        # verb at all -- must keep routing to EXPLAIN_BITRIX_WRITE_PLAN.
        self.assertFalse(is_explicit_product_enrichment_request(TURN2_TEXT))
        self.assertTrue(is_bitrix_write_plan_question(TURN2_TEXT))

    def test_single_component_noun_alone_does_not_force_enrichment(self):
        # Only ONE enrichment-component noun (no "full card" phrase
        # either) must not be enough to force this narrow predicate.
        self.assertFalse(
            is_explicit_product_enrichment_request("Подготовь товар: обнови характеристики.")
        )


class EnrichSameProductContinuationDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the EXACT production 3-turn scenario end to end through
    ``BusinessAssistantApiService`` (the same object the HTTP API's
    ``POST /api/v1/business-assistant/requests`` handler uses), with a
    REAL ``WorkflowPandaConversationGateway`` wired to a LIVE-mode Bitrix
    bridge (mocked HTTP transport only), exactly like PR #68's own test."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "enrich_same_product.sqlite")
        self.transport = _RecordingTransport(sections=[TV_SECTION])
        self.live_env = _LiveEnv()
        self.live_env.__enter__()
        self.http_patch = patch.object(BoundedHttpClient, "request", side_effect=self.transport)
        self.http_patch.start()
        self.bridge, _activation = _bridge_and_activation()

        svc = DataIntelligenceService(InMemoryDatasetStore())
        self.artifact_service = ArtifactService(store=InMemoryArtifactStore())
        svc.artifact_service = self.artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=svc)
        gateway = ToolGateway(registry=registry, register_search=False)
        self.conversation_gateway = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=gateway,
            artifact_service=self.artifact_service,
            bitrix_product_bridge=self.bridge,
        )
        self.rt = build_business_assistant_api_runtime(
            db_path=self.db,
            conversation_gateway=self.conversation_gateway,
            artifact_service=self.artifact_service,
        )
        self.svc = self.rt.service

    async def asyncTearDown(self):
        self.rt.close()
        self.http_patch.stop()
        self.live_env.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_enrich_same_product_follow_up_invokes_enrichment_not_write_plan(self):
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-1"
        )

        # Turn 1: XLSX attached to THIS turn -- plain row-preview (PR #68).
        turn1 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-1",
            idempotency_key="enrich-same-product-turn1",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)
        result1 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id)
        self.assertIn(TARGET_SKU, result1["summary"])
        self.assertIn("не выполнена", result1["summary"])

        active_after_turn1 = self.conversation_gateway._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        self.assertEqual(active_after_turn1.family, FAMILY_EXCEL)
        dataset_id_after_turn1 = str(active_after_turn1.parameters.get("dataset_id") or "")
        self.assertTrue(dataset_id_after_turn1)

        # Turn 2: no attachment -- PR #67/#68's own "final plan" follow-up
        # must still resolve from the row-preview state alone.
        turn2 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN2_TEXT,
            conversation_id="conv-1",
            idempotency_key="enrich-same-product-turn2",
        )
        self.assertEqual(turn2.status, ST_COMPLETED)
        result2 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        summary2 = result2["summary"]
        self.assertIn(WRITE_PLAN_HEADER, summary2)
        self.assertIn(TARGET_SKU, summary2)
        self.assertIn(TARGET_RETAIL_PRICE, summary2)
        self.assertIn(str(TV_SECTION_ID), summary2)

        active_after_turn2 = self.conversation_gateway._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        # PRODUCT-FIRST DEFECT CLOSURE: a bare "show the final write plan"
        # ask -- with no "enrichment"/"обогащение" wording at all -- is now
        # the SAME site-ready-card business intent as an explicit
        # enrichment request (see ``WorkflowPandaConversationGateway.
        # _auto_prepare_site_ready_card_if_needed``), so Turn 2 already
        # auto-prepares and persists the complete card here. This used to
        # assert the opposite (the exact production defect this later fix
        # closes) -- Turn 3's own explicit "Выполни обогащение..." request
        # below still reruns/re-persists enrichment regardless, so this
        # updated expectation changes nothing about Turn 3's own assertions.
        turn2_enrichment_write_request = dict(
            active_after_turn2.parameters.get("bitrix_enrichment_write_request") or {}
        )
        self.assertTrue(turn2_enrichment_write_request)
        self.assertEqual(turn2_enrichment_write_request.get("sku"), TARGET_SKU)

        # Turn 3: the EXACT reproduced defect -- explicit enrich/prepare
        # ACTION for the SAME product, no attachment.
        turn3 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN3_TEXT,
            conversation_id="conv-1",
            idempotency_key="enrich-same-product-turn3",
        )
        self.assertEqual(
            turn3.status,
            ST_COMPLETED,
            "turn 3 must complete through the conversational pipeline, never BLOCKED",
        )
        result3 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn3.request_id)
        summary3 = result3["summary"]

        # Must invoke CALL_PRODUCT_ENRICHMENT's own rendering -- never the
        # NON-enriched write-plan render (the exact reported symptom) and
        # never the generic legacy business workflow.
        self.assertNotIn(NON_ENRICHED_MARKER, summary3)
        self.assertNotIn(WRITE_PLAN_HEADER, summary3)
        self.assertIn(ENRICHMENT_PREVIEW_HEADER, summary3)
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary3)

        # The SAME prepared product -- no reattachment, no second Excel
        # ingestion (same dataset_id throughout), existing retail-price
        # and Bitrix section resolver both preserved.
        self.assertIn(TARGET_SKU, summary3)
        self.assertIn(TARGET_EAN, summary3)
        self.assertIn(TARGET_RETAIL_PRICE, summary3)
        self.assertIn(str(TV_SECTION_ID), summary3)

        active_after_turn3 = self.conversation_gateway._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        self.assertEqual(str(active_after_turn3.parameters.get("dataset_id") or ""), dataset_id_after_turn1)

        # Enriched canonical state IS persisted (characteristics/
        # descriptions/media become available whenever the underlying
        # enrichment/search/media pipeline itself returns them -- never
        # invented here).
        enrichment_write_request = dict(active_after_turn3.parameters.get("bitrix_enrichment_write_request") or {})
        self.assertTrue(enrichment_write_request)
        self.assertEqual(enrichment_write_request.get("sku"), TARGET_SKU)

        # ZERO Bitrix mutations anywhere across all three turns -- only
        # the read-only catalog.section.list resolver call happens.
        methods_called = [m for m, _ in self.transport.calls]
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)
        self.assertIn("catalog.section.list", methods_called)


if __name__ == "__main__":
    unittest.main()
