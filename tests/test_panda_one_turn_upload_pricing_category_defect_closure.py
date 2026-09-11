"""PANDA — PRODUCTION DEFECT CLOSURE: single-turn "upload XLSX + take the
first product + calculate retail price + resolve exact Bitrix/Aspro
category" request loses the SAME-turn attachment context and incorrectly
answers with the "no prepared card" fail-closed message.

Reproduced production defect (exact ONE-TURN scenario, in a BRAND-NEW
conversation -- no prior turns at all):

    User sends ONE message with LG_TV.xlsx attached: "Возьми первый товар
    из загруженного LG_TV.xlsx и подготовь его для Bitrix/Aspro. Покажи в
    чате название, артикул, EAN, бренд, закупочную цену, рассчитанную
    розничную цену, точную категорию Bitrix/Aspro и план записи в
    Bitrix. Ничего не записывай и не публикуй в Bitrix без моего
    отдельного подтверждения."

    Production symptom: Panda replied with the generic "Не вижу
    подготовленной карточки товара для записи в Bitrix. Сначала
    приложите файл и попросите подготовить карточку конкретного товара,
    а затем подтвердите его создание." -- even though LG_TV.xlsx IS
    attached to that same message.

Root cause: PR #63's new
``business_assistant.action_continuation.is_explicit_product_pricing_or_
category_refinement_request`` predicate matches this message too (it
names a pricing-calculation verb -- "рассчитанную" -- plus a Bitrix/Aspro
target), so ``resolve_action_turn`` dispatched it straight to PR #63's
new ``resolve_product_pricing_category_refinement_request``, which
requires an ALREADY-EXISTING ``FAMILY_EXCEL`` active task (the SAME
product a PRIOR turn already selected) and unconditionally failed closed
with the missing-context message on its ``active is None`` guard --
never noticing that THIS SAME turn also carries the XLSX attachment
that should be ingested first. PR #63's predicate/resolver never
accounted for the "attachment and instruction arrive in the SAME first
message of a brand-new conversation" case -- exactly the same class of
one-turn gap ``_enrichment_needs_excel_ingestion_first`` already closes
for the analogous product-enrichment predicate.

Fix: a new, narrow, additive helper
``_pricing_category_refinement_needs_excel_ingestion_first`` (mirrors
``_enrichment_needs_excel_ingestion_first`` exactly) detects this exact
one-turn case and resolves THIS turn as an ordinary ``FAMILY_EXCEL`` turn
first (ingesting the attached workbook and selecting the first row via
PR #62's own "first product" fallback), then chains straight into
``EXPLAIN_BITRIX_WRITE_PLAN`` within the SAME turn via a new
``ActionDecision.chain_to_pricing_category_refinement_text`` field and a
new ``WorkflowPandaConversationGateway._maybe_chain_to_pricing_category_
refinement`` method -- both mirroring the existing ``chain_to_enrichment_
text``/``_maybe_chain_to_enrichment`` pattern exactly. Additionally,
``business_assistant.product_enrichment_bridge.format_write_plan_text``
now also renders the purchase price VALUE (mirroring its existing
"Розничная цена" line) so the rendered card shows every field this
production request asked for; this line was simply never populated
before because no caller previously needed it. No other rendering
changed.

This test runs the REAL production stack end-to-end through
``BusinessAssistantApiService`` with a REAL
``WorkflowPandaConversationGateway``, wired to a Bitrix bridge configured
LIVE against a mocked HTTP transport (mirrors PR #63's own test's
established, zero-real-network pattern) -- so the real category resolver
(``schema.resolve_section_id``) runs end to end, and the recording
transport raises on any unexpected call (including
``catalog.product.add``), guaranteeing ZERO real Bitrix mutation.
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
    is_explicit_product_pricing_or_category_refinement_request,
)
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant.intent import classify_intent, is_conversational
from business_assistant.models import INTENT_CONVERSATIONAL
from business_assistant_api.models import ST_COMPLETED
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from integrations.production.http import BoundedHttpClient
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

from tests.test_bitrix_live_product_create_write import (
    _bridge_and_activation,
    _LiveEnv,
    _RecordingTransport,
)

TARGET_SKU = "55MRGB86B6A.ARUG"
TARGET_EAN = "8806096824788"
FILENAME = "LG_TV.xlsx"

PURCHASE_PRICE = "103198.3"
# Sourced from the workbook's own "розница" column -- the existing Panda
# pricing path reused, never a new/invented calculation (see PR #63's own
# regression test for the same reasoning).
RETAIL_PRICE = "119990"

TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# The EXACT reproduced production request -- ONE message, in a BRAND-NEW
# conversation, XLSX attached to THIS SAME turn.
ONE_TURN_TEXT = (
    "Возьми первый товар из загруженного LG_TV.xlsx и подготовь его для "
    "Bitrix/Aspro. Покажи в чате название, артикул, EAN, бренд, "
    "закупочную цену, рассчитанную розничную цену, точную категорию "
    "Bitrix/Aspro и план записи в Bitrix. Ничего не записывай и не "
    "публикуй в Bitrix без моего отдельного подтверждения."
)

# The exact reported production symptom -- must never appear again.
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

# The exact generic-spreadsheet-statistics shape this request must never
# fall back to either.
GENERIC_STATS_MARKER = "столбцов."


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append(
        [TARGET_SKU, f"LG {TARGET_SKU}", "Телевизоры", "LG", TARGET_EAN, PURCHASE_PRICE, RETAIL_PRICE]
    )
    ws.append(["OTHER-SKU", "Samsung Other TV", "Телевизоры", "Samsung", "1234567890123", "50000", "65000"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class OneTurnPricingCategoryRoutingUnitTests(unittest.TestCase):
    """Pins the exact predicate/routing signal in isolation."""

    def test_one_turn_text_is_explicit_pricing_or_category_refinement_request(self):
        self.assertTrue(is_explicit_product_pricing_or_category_refinement_request(ONE_TURN_TEXT))

    def test_one_turn_text_routes_conversational_with_attachment(self):
        self.assertTrue(is_conversational(ONE_TURN_TEXT, has_attachments=True))
        self.assertEqual(classify_intent(ONE_TURN_TEXT, has_attachments=True), INTENT_CONVERSATIONAL)


class OneTurnUploadPricingCategoryDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the EXACT one-turn production scenario end-to-end via the
    same ``BusinessAssistantApiService`` the HTTP API uses, in a BRAND-NEW
    conversation (no prior turns), with a REAL
    ``WorkflowPandaConversationGateway`` wired to a Bitrix bridge
    configured LIVE against a mocked HTTP transport -- zero real network
    calls, zero real Bitrix mutations (the recording transport raises on
    any unexpected call, including ``catalog.product.add``)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_one_turn_defect.sqlite")
        self.transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
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

    async def test_single_turn_upload_and_prepare_request_shows_the_card_not_the_missing_context_error(self):
        """The ONE-TURN production request must resolve the first product
        from the SAME-turn attachment and show its prepared card + write
        plan -- never the generic "no prepared card" fail-closed message,
        never the legacy business workflow, never generic spreadsheet
        stats, and never a Bitrix mutation."""
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-new"
        )

        req = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=ONE_TURN_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-new",
            idempotency_key="one-turn-defect-1",
        )
        self.assertEqual(
            req.status,
            ST_COMPLETED,
            "the single upload+prepare turn must complete through the conversational pipeline",
        )
        result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=req.request_id)
        summary = result["summary"]

        # The exact reported production symptom must never reappear.
        self.assertNotIn(
            MISSING_CONTEXT_MARKER,
            summary,
            "regressed into the 'no prepared card' fail-closed message despite the SAME-turn attachment",
        )
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary, f"degraded into the generic business workflow (found {marker!r})")
        self.assertNotIn(GENERIC_STATS_MARKER, summary, "must not fall back to generic spreadsheet statistics")

        # The FIRST product's prepared card, shown directly in chat.
        self.assertIn(TARGET_SKU, summary)  # article/SKU
        self.assertIn(TARGET_EAN, summary)  # EAN
        self.assertIn("LG", summary)  # brand
        self.assertIn(PURCHASE_PRICE, summary)  # purchase price
        self.assertIn(RETAIL_PRICE, summary)  # calculated/known retail price

        # The exact Bitrix/Aspro category, resolved through the REAL
        # (mocked) category resolver end to end.
        self.assertIn(str(TV_SECTION_ID), summary)

        # The read-only write plan, rendered in chat -- never actually
        # written.
        self.assertIn("Ничего в Bitrix не записано", summary)

        # ZERO Bitrix mutation: the only mocked Bitrix call made anywhere
        # in this test is the read-only ``catalog.section.list`` -- never
        # a ``catalog.product.add`` (the recording transport would raise
        # ``AssertionError`` on any other unexpected call).
        methods_called = [m for m, _ in self.transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)


if __name__ == "__main__":
    unittest.main()
