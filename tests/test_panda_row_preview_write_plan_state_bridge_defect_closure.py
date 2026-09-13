"""Production defect closure: after PR #67's routing fix, the read-only
"show me the FINAL plan for this same product" follow-up still fails when
Turn 1 only ran the plain row-preview/single-product Bitrix-prep flow
(never ``CALL_PRODUCT_ENRICHMENT``) -- the EXACT production shape reported
after PR #67 shipped.

Exact reproduced production shape (conversation
``b8f5ae10-5f3a-465e-ac93-58c90b93da27``, request ids
``5c1e6b9a-f685-489b-a784-1b4dabee713e`` / ``5b710fd7-3e83-48c5-b461-
702a07883aff``):

    Turn 1 (XLSX attached to THIS turn): "Прикрепляю LG_TV.xlsx. Подготовь
        первый товар из файла для Bitrix/Aspro." -> the plain
        ``data_intel.service._row_lookup_result`` ROW_FOUND preview (NOT
        enrichment): "Карточка товара (предпросмотр): ... Статус:
        подготовлено для предпросмотра Bitrix/Aspro. Публикация/запись не
        выполнена — жду вашего подтверждения перед записью."

    Turn 2 (NO new attachment -- the XLSX was already processed in Turn
        1): "Покажи перед подтверждением записи окончательный план для
        этого же товара: закупочную цену, розничную цену, точный раздел
        Bitrix/Aspro с названием и ID, EAN, артикул, количество
        характеристик, основное изображение и количество изображений
        галереи. Ничего пока не записывай и не публикуй."

DIAGNOSIS (proven by reproduction below, not guessed):

1. Turn 1's ROW_FOUND preview persists ONLY ``bitrix_product_fields``
   (title/sku/ean/category/brand/purchase_price) and
   ``bitrix_retail_price_preview`` on the conversation's FAMILY_EXCEL
   ``ActiveTask`` (``WorkflowPandaConversationGateway._invoke_tool``'s
   existing FAMILY_EXCEL/ROW_FOUND block) -- it never runs
   ``CALL_PRODUCT_ENRICHMENT``, so ``bitrix_enrichment_write_request``/
   ``bitrix_enrichment_preview``/``bitrix_enrichment_characteristic_
   status`` are never set.

2. PR #67 correctly routes Turn 2 to ``is_bitrix_write_plan_question`` ->
   ``resolve_bitrix_write_plan_question`` (never touched again here).

3. THE MISMATCH: pre-this-fix, ``resolve_bitrix_write_plan_question``
   checked ONLY ``bitrix_enrichment_write_request`` and, finding it empty,
   fell back to ``_bitrix_missing_context_decision`` -- the EXACT reported
   "Не вижу подготовленной карточки товара для записи в Bitrix. Сначала
   приложите файл и попросите подготовить карточку конкретного товара, а
   затем подтвердите его создание." -- even though ``bitrix_product_
   fields``/``bitrix_retail_price_preview`` (a perfectly usable prepared-
   product state) was sitting right there on the SAME active task.
   ``resolve_product_pricing_category_refinement_request`` already had the
   exact fallback needed (``build_write_request_from_fields`` from
   ``bitrix_product_fields``) -- ``resolve_bitrix_write_plan_question``
   simply never used it.

FIX (this change set): ``resolve_bitrix_write_plan_question`` now falls
back to the SAME existing, already-proven ``build_write_request_from_
fields`` construction whenever the richer enrichment state is absent but
``bitrix_product_fields`` is present on a FAMILY_EXCEL task -- reusing the
EXISTING canonical mechanism, never inventing a new one, never inventing
missing field values (characteristics/images/description stay honestly
empty when Turn 1 never enriched them).

Confirmed via ``git stash`` that this exact test fails pre-fix with the
verbatim reported "Не вижу подготовленной карточки..." text, and passes
post-fix.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import FAMILY_EXCEL, ActiveTaskStore
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

# Exact production values (from the reported reproduction).
TARGET_SKU = "100MRGB96B6.ARUG"
TARGET_TITLE = "Телевизор LG 100MRGB96B6.ARUG"
TARGET_EAN = "8806096796849"
TARGET_BRAND = "LG"
TARGET_PURCHASE_PRICE = "717790.3"
# A bare "цена" (role ROLE_PRICE) column -- PR #66's restored fallback --
# so the write plan's retail price is populated too (production's own
# Turn 1 showed no "Розничная цена" line at all -- this test additionally
# proves the PR #66 path is intact end to end through this new fallback).
TARGET_RETAIL_PRICE = "799990"
FILENAME = "LG_TV.xlsx"

TV_SECTION_ID = 70
TV_SECTION_NAME = "Телевизоры"
TV_SECTION = {"id": TV_SECTION_ID, "name": TV_SECTION_NAME, "code": "televizory"}

# Turn 1: the EXACT reproduced production request -- ONE message, XLSX
# attached to THIS SAME turn, asks Panda to prepare a specific (first)
# product for Bitrix/Aspro. Deliberately NOT an enrichment request (no
# "подготовь ПОЛНУЮ карточку") -- this is the plain row-preview/single-
# product Bitrix-prep shape that never runs CALL_PRODUCT_ENRICHMENT.
TURN1_TEXT = "Прикрепляю LG_TV.xlsx. Подготовь первый товар из файла для Bitrix/Aspro."

# Turn 2: the EXACT reproduced production follow-up (PR #67's own
# "final plan" phrasing) -- SAME conversation, NO new attachment.
TURN2_TEXT = (
    "Покажи перед подтверждением записи окончательный план для этого же товара: "
    "закупочную цену, розничную цену, точный раздел Bitrix/Aspro с названием и ID, "
    "EAN, артикул, количество характеристик, основное изображение и количество "
    "изображений галереи. Ничего пока не записывай и не публикуй."
)

# The exact reported production symptom -- must never appear again once
# Turn 1's prepared state exists.
MISSING_CONTEXT_MARKER = "Не вижу подготовленной карточки товара"

# Markers that only ever appear in the LEGACY fixture business-workflow's
# diagnostic summary -- never in a conversational ConversationResult.text.
WORKFLOW_DIAGNOSTIC_MARKERS = (
    "Задача выполнена",
    "Подробности доступны",
    "Requested:",
    "Findings:",
    "Fixture_mode:",
    "Waiting_approval:",
)


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "цена"])
    ws.append(
        [TARGET_SKU, TARGET_TITLE, TV_SECTION_NAME, TARGET_BRAND, TARGET_EAN, TARGET_PURCHASE_PRICE, TARGET_RETAIL_PRICE]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class RowPreviewWritePlanStateBridgeUnitTests(unittest.IsolatedAsyncioTestCase):
    """Pins the exact persisted-state mismatch at the ActiveTask level,
    independent of the full API-layer reproduction below."""

    async def test_row_preview_persists_product_fields_but_not_enrichment_state(self):
        svc = DataIntelligenceService(InMemoryDatasetStore())
        artifact_service = ArtifactService(store=InMemoryArtifactStore())
        svc.artifact_service = artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=svc)
        gateway = ToolGateway(registry=registry, register_search=False)
        panda = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=gateway,
            artifact_service=artifact_service,
        )
        rec = artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="u1", filename=FILENAME, content=_xlsx_bytes()
        )
        artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="c1"
        )
        from business_assistant.conversation_gateway import ConversationRequest

        await panda.respond(
            ConversationRequest(
                text=TURN1_TEXT,
                tenant_id="tenant-a",
                user_id="u1",
                request_id="r1",
                conversation_id="c1",
                attachment_refs=(rec.artifact_id,),
            )
        )
        store: ActiveTaskStore = panda._action_store  # noqa: SLF001
        active = store.get(tenant_id="tenant-a", owner_id="u1", conversation_id="c1")
        self.assertIsNotNone(active)
        self.assertEqual(active.family, FAMILY_EXCEL)
        fields = dict(active.parameters.get("bitrix_product_fields") or {})
        self.assertEqual(fields.get("sku"), TARGET_SKU)
        self.assertEqual(fields.get("title"), TARGET_TITLE)
        self.assertEqual(str(active.parameters.get("bitrix_retail_price_preview") or ""), TARGET_RETAIL_PRICE)
        # The exact absence that caused the defect: no enrichment state.
        self.assertFalse(dict(active.parameters.get("bitrix_enrichment_write_request") or {}))


class RowPreviewWritePlanStateBridgeDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the EXACT production 2-turn scenario end to end through
    ``BusinessAssistantApiService`` (the same object the HTTP API's
    ``POST /api/v1/business-assistant/requests`` handler uses), with a
    REAL ``WorkflowPandaConversationGateway`` wired to a LIVE-mode Bitrix
    bridge (mocked HTTP transport only) so the REAL category resolver
    (``schema.resolve_section_id``) runs end to end, exactly like PR #66's
    own control-product test."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "row_preview_write_plan_bridge.sqlite")
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

    async def test_final_plan_follow_up_reuses_row_preview_state_without_reattachment(self):
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-1"
        )
        before_calls = len(self.transport.calls)

        # Turn 1: XLSX attached to THIS turn, plain row-preview/single-
        # product Bitrix-prep request (never enrichment).
        turn1 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-1",
            idempotency_key="row-preview-bridge-turn1",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)
        result1 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id)
        summary1 = result1["summary"]
        self.assertIn(TARGET_SKU, summary1)
        self.assertIn(TARGET_EAN, summary1)
        self.assertIn(TARGET_PURCHASE_PRICE, summary1)
        self.assertIn(TARGET_RETAIL_PRICE, summary1)  # PR #66's restored retail-price fallback.
        self.assertIn("не выполнена", summary1)  # "write not performed yet"

        # Confirms the EXACT persisted-state mismatch this fix bridges.
        active = self.conversation_gateway._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
        )
        self.assertEqual(active.family, FAMILY_EXCEL)
        self.assertTrue(dict(active.parameters.get("bitrix_product_fields") or {}))
        self.assertFalse(dict(active.parameters.get("bitrix_enrichment_write_request") or {}))

        # Turn 2: SAME conversation, NO new attachment -- PR #67's own
        # "final plan" phrasing.
        turn2 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN2_TEXT,
            conversation_id="conv-1",
            idempotency_key="row-preview-bridge-turn2",
        )
        self.assertEqual(turn2.status, ST_COMPLETED)
        result2 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        summary2 = result2["summary"]

        # Never the exact reported production symptom again.
        self.assertNotIn(MISSING_CONTEXT_MARKER, summary2)
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary2)

        # The SAME prepared product, reused -- no reattachment, no second
        # ingestion (only ONE dataset_id exists throughout), no enrichment
        # rerun (characteristics/description/images stay honestly empty --
        # Turn 1 never enriched them; never invented here).
        self.assertIn(TARGET_SKU, summary2)
        self.assertIn(TARGET_EAN, summary2)
        self.assertIn(TARGET_BRAND, summary2)
        self.assertIn(TARGET_PURCHASE_PRICE, summary2)
        self.assertIn(TARGET_RETAIL_PRICE, summary2)
        self.assertIn(str(TV_SECTION_ID), summary2)  # exact resolved Bitrix/Aspro section ID
        self.assertIn(TV_SECTION_NAME, summary2)  # exact resolved Bitrix/Aspro section name
        self.assertIn("ХАРАКТЕРИСТИКИ, КОТОРЫЕ БУДУТ ЗАПИСАНЫ: 0", summary2)
        self.assertIn("нет подготовленных изображений", summary2)
        self.assertIn("Ничего в Bitrix не записано: это только предпросмотр.", summary2)

        # ZERO Bitrix mutation from preparation/explanation alone -- the
        # only Bitrix call made anywhere is the read-only
        # ``catalog.section.list``.
        methods_called = [m for m, _ in self.transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)
        self.assertGreaterEqual(len(self.transport.calls), before_calls)

        # No SECOND Excel ingestion happened -- the SAME dataset_id
        # persists across both turns.
        self.assertEqual(
            str(active.parameters.get("dataset_id") or ""),
            str(
                self.conversation_gateway._action_store.get(  # noqa: SLF001
                    tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1"
                ).parameters.get("dataset_id")
                or ""
            ),
        )


if __name__ == "__main__":
    unittest.main()
