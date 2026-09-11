"""PANDA — PRODUCTION DEFECT CLOSURE: single-product Bitrix preparation
follow-up (no new attachment) loses the user's instruction and falls back
to generic spreadsheet analysis / the degraded legacy business workflow.

Reproduced production defect (exact 3-turn scenario):

    Turn 1: user uploads LG_TV.xlsx and asks Panda, generically, to prepare
            a product for Bitrix/Aspro. Panda replies with only generic
            spreadsheet statistics ("N rows, M columns, price min/max/avg").
            (Acceptable on its own -- the request named no specific item.)

    Turn 2 (SAME conversation, NO new attachment): "Do not analyze the
            whole spreadsheet. Choose ONE first product from LG_TV.xlsx
            and prepare it for Bitrix/Aspro: exact model, EAN, brand,
            category, purchase price, retail price, characteristics,
            description, images, and show the Bitrix write plan. Do not
            write/publish yet."

            Production symptom: Panda replied with a generic "task
            completed, see management" message (the legacy fixture
            business-workflow's diagnostic summary), never surfacing any
            product card.

    Turn 3: "Show the result in the chat." Production symptom: Panda
            AGAIN returned the same generic spreadsheet statistics from
            turn 1 instead of the product/Bitrix plan.

Root causes (two independent points on the SAME reproducible path, both
required to close the defect -- see the two focused fixes below):

1. ``business_assistant.intent.requires_business_integration`` matches the
   turn-2 wording (mentions the workbook filename/"xlsx" -- one of its own
   ``_BUSINESS_TASK_KEYWORDS`` -- plus "Bitrix" and a preparation verb)
   whenever the follow-up turn itself carries no ``artifact_refs`` (the
   file was uploaded on an earlier turn). ``is_conversational`` then
   routed it to the attachment-blind legacy ``BusinessAssistantService.
   execute()`` recipe engine instead of ``WorkflowPandaConversationGateway``,
   which is the only pipeline that resolves the already-uploaded dataset.
   Fix: ``business_assistant.action_continuation.
   is_explicit_single_product_bitrix_prep_request`` (new, narrow, additive
   predicate mirroring the existing ``is_explicit_product_enrichment_
   request``/``is_bitrix_write_plan_question`` precedent) now recognizes
   this exact "select exactly one product, don't analyze the whole
   spreadsheet, prepare it for Bitrix/Aspro" shape and
   ``business_assistant.intent.is_conversational`` routes it to the
   conversational pipeline.

2. Even once correctly routed, ``data_intel.service.
   DataIntelligenceService.execute_nl_request``'s existing row-lookup
   fallback (``_find_row_by_identifier``) only ever matched a row when the
   free text named a CONCRETE identifier substring (SKU/EAN/product name)
   -- "choose ONE first product" names none, so it still fell through to
   ``_analyze_only_summary``'s dimension-only text. Fix: a new, narrow
   ``_wants_first_row``/``_first_row_hit`` fallback -- reached ONLY when no
   concrete identifier matched AND the text explicitly asks to select the
   first/one product -- resolves the table's own first row through the
   SAME existing ``_row_lookup_result`` preview (schema-driven, zero new
   parsing, zero Bitrix mutation, write still requires separate
   confirmation).

This test exercises the REAL production stack end-to-end through
``BusinessAssistantApiService`` (the same object the HTTP API's
``POST /api/v1/business-assistant/requests`` handler uses) with a REAL
``WorkflowPandaConversationGateway`` wired to a fixture Bitrix bridge, so a
regression in either fix would be caught even if intent/routing or
row-lookup were tested in isolation.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import (
    is_explicit_single_product_bitrix_prep_request,
)
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant.intent import classify_intent, is_conversational
from business_assistant.models import INTENT_CONVERSATIONAL
from business_assistant_api.models import ST_COMPLETED
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.catalog import BitrixCatalogStore
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter
from integrations.bitrix.product_bridge import BitrixProductBridge
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

TARGET_SKU = "55MRGB86B6A.ARUG"
TARGET_EAN = "8806096824788"
FILENAME = "LG_TV.xlsx"

# Turn 1: generic upload + generic prepare-for-Bitrix ask (no specific item
# named) -- generic spreadsheet statistics is an acceptable reply to THIS
# turn on its own; it is turn 2/3 that must not regress into it.
TURN1_TEXT = "Проанализируй загруженный прайс и подготовь товар для Bitrix/Aspro."

# Turn 2: the EXACT reproduced production request -- SAME conversation, NO
# artifact_refs on this specific message (the file was uploaded on turn 1).
TURN2_TEXT = (
    "Do not analyze the whole spreadsheet. Choose ONE first product from "
    f"{FILENAME} and prepare it for Bitrix/Aspro: exact model, EAN, brand, "
    "category, purchase price, retail price, characteristics, description, "
    "images, and show the Bitrix write plan. Do not write/publish yet."
)

TURN3_TEXT = "Show the result in the chat."

# Markers that only ever appear in the LEGACY fixture business-workflow's
# diagnostic summary (business_assistant.service.BusinessAssistantService.
# _compose_summary) -- never in a conversational ConversationResult.text.
# Their presence would mean turn 2/3 degraded to the generic workflow.
WORKFLOW_DIAGNOSTIC_MARKERS = ("Requested:", "Findings:", "Fixture_mode:", "Waiting_approval:")

# The exact generic-spreadsheet-statistics shape (see
# ``data_intel.service.DataIntelligenceService._analyze_only_summary``)
# that turn 2/3 must never regress back into.
GENERIC_STATS_MARKER = "столбцов."


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price"])
    ws.append([TARGET_SKU, f"LG {TARGET_SKU}", "TV", "LG", TARGET_EAN, "103198.3"])
    ws.append(["OTHER-SKU", "Samsung Other TV", "TV", "Samsung", "1234567890123", "50000"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _bitrix_bridge() -> tuple[BitrixProductBridge, BitrixCatalogStore]:
    store = BitrixCatalogStore()
    activation = IntegrationActivationService()
    adapter = BitrixFixtureAdapter(store=store)
    activation._adapters["bitrix"] = adapter  # noqa: SLF001
    ref = activation.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:bitrix-tenant-a", value="tok")
    conn = activation.configure_connection(
        tenant_id="tenant-a", provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
    )
    activation.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    activation.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
    bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)
    return bridge, store


class SingleProductBitrixPrepRoutingUnitTests(unittest.TestCase):
    """Pins fix #1 (intent/routing layer) in isolation."""

    def test_turn2_text_is_explicit_single_product_bitrix_prep_request(self):
        self.assertTrue(is_explicit_single_product_bitrix_prep_request(TURN2_TEXT))

    def test_turn2_text_routes_conversational_without_attachment(self):
        self.assertTrue(is_conversational(TURN2_TEXT, has_attachments=False))
        self.assertEqual(classify_intent(TURN2_TEXT, has_attachments=False), INTENT_CONVERSATIONAL)

    def test_bare_prepare_verb_alone_is_unaffected(self):
        # A bare "подготовь товар для Bitrix" (turn 1's own generic ask,
        # no attachment) must NOT be force-routed by the new predicate --
        # it names no single-item selection signal at all.
        self.assertFalse(is_explicit_single_product_bitrix_prep_request(TURN1_TEXT))

    def test_explicit_write_confirmation_still_wins_over_new_predicate(self):
        # An unambiguous write confirmation must never be reclassified by
        # this predicate (mirrors the existing enrichment/write-plan guards).
        confirm_text = (
            "Подтверждаю: создай этот товар в Bitrix. Выбери первый товар. Розничная цена 29990 \u20bd."
        )
        self.assertFalse(is_explicit_single_product_bitrix_prep_request(confirm_text))


class SingleProductBitrixPrepFollowUpDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the EXACT production 3-turn scenario end-to-end via the
    same ``BusinessAssistantApiService`` the HTTP API uses, with a REAL
    ``WorkflowPandaConversationGateway`` (never a fake double)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_single_prod_prep_defect.sqlite")
        self.bridge, self.store = _bitrix_bridge()

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
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_upload_persists_usable_file_context(self):
        """Regression 1: the XLSX upload turn must persist a dataset the
        conversation can keep referring to WITHOUT a new attachment."""
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-1"
        )
        turn1 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-1",
            idempotency_key="single-prod-defect-turn1",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)
        result1 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id)
        # Turn 1's own generic ask legitimately gets the dimension-only
        # summary -- this pins that this specific turn's shape is
        # unaffected, while still proving a dataset now exists to reuse.
        self.assertIn(GENERIC_STATS_MARKER, result1["summary"])

        from business_assistant.action_continuation import ActiveTaskStore, FAMILY_EXCEL

        store = self.conversation_gateway._action_store  # noqa: SLF001
        self.assertIsInstance(store, ActiveTaskStore)
        active = store.get(tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-1")
        self.assertIsNotNone(active)
        self.assertEqual(active.family, FAMILY_EXCEL)
        self.assertTrue(str(active.parameters.get("dataset_id") or ""))

    async def test_full_reproduction_three_turns(self):
        """Regressions 2 + 3: the follow-up must NOT route to the generic
        business workflow or the generic spreadsheet summary, must render
        the product/Bitrix plan in chat, and must never write to Bitrix
        before an explicit confirmation."""
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-1"
        )
        before_catalog_size = len(self.store.catalog("tenant-a"))

        turn1 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-1",
            idempotency_key="single-prod-defect-turn1b",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)

        # Turn 2: SAME conversation, NO artifact_refs on this message.
        turn2 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN2_TEXT,
            conversation_id="conv-1",
            idempotency_key="single-prod-defect-turn2",
        )
        self.assertEqual(
            turn2.status,
            ST_COMPLETED,
            "turn 2 must complete through the conversational pipeline, never BLOCKED by the "
            "attachment-blind legacy business workflow",
        )
        result2 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        summary2 = result2["summary"]
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary2, f"turn2 degraded into the generic business workflow (found {marker!r})")
        self.assertNotIn(
            GENERIC_STATS_MARKER, summary2, "turn2 must not fall back to the generic spreadsheet summary"
        )
        # The real product card + Bitrix preparation status, rendered in chat.
        self.assertIn(TARGET_SKU, summary2)
        self.assertIn(TARGET_EAN, summary2)
        self.assertIn("Bitrix/Aspro", summary2)
        self.assertIn("не выполнена", summary2)  # "write not performed yet"

        # ZERO Bitrix mutation from preparation alone.
        self.assertEqual(len(self.store.catalog("tenant-a")), before_catalog_size)

        # Turn 3: "Show the result in the chat." -- must keep surfacing the
        # SAME prepared product/Bitrix plan, never regress to generic stats.
        turn3 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN3_TEXT,
            conversation_id="conv-1",
            idempotency_key="single-prod-defect-turn3",
        )
        self.assertEqual(turn3.status, ST_COMPLETED)
        result3 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn3.request_id)
        summary3 = result3["summary"]
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary3, f"turn3 degraded into the generic business workflow (found {marker!r})")
        self.assertNotIn(
            GENERIC_STATS_MARKER, summary3, "turn3 must not fall back to the generic spreadsheet summary again"
        )
        self.assertIn(TARGET_SKU, summary3)

        # STILL zero Bitrix mutation -- no write happened across any of the
        # three turns without an explicit confirmation.
        self.assertEqual(len(self.store.catalog("tenant-a")), before_catalog_size)


if __name__ == "__main__":
    unittest.main()
