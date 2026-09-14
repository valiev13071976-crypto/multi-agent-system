"""PANDA — PRODUCTION DEFECT CLOSURE: product-workflow conversation
continuity across attachment-less follow-up turns.

==================================================
PROVEN PRODUCTION DEFECT
==================================================

TURN 1 (SAME conversation): user attaches an XLSX price list and asks
Panda to prepare one product. This works: Panda reads the workbook,
selects a product, enriches/previews it, and keeps the write
unconfirmed.

TURN 2 (SAME conversation, NO new attachment -- the file was already
uploaded on turn 1): the user says, in natural language, e.g. "Этот
товар уже был. Возьми другой телевизор." (no SKU/EAN named, no
"Bitrix" word, no attachment). Production symptom: Panda returned the
generic "Задача выполнена. Подробности доступны в разделе управления."
diagnostic instead of continuing the SAME product task, with
diagnostics showing ``attachment_count = 0``,
``ba_status = COMPLETED_WITH_WARNINGS`` and blocked-step codes
``BA_CAPABILITY_UNAVAILABLE`` / ``dependency_not_ready``.

==================================================
ROOT CAUSE (proven from the code path, not guessed)
==================================================

1. Turn-1 attachment metadata lives in ``ArtifactService``
   (``artifacts/service.py``), linked to ``conversation_id`` via
   ``attach_to_conversation`` -- durable, keyed by conversation, never
   re-sent by the client on later turns.
2. Turn-1's PARSED XLSX (rows/columns/roles) is registered by
   ``DataIntelligenceService`` (``data_intel/service.py``) into its
   dataset store and referenced by a ``dataset_id`` string.
3. The ACTIVE product/XLSX task (``FAMILY_EXCEL``) is stored by
   ``business_assistant.action_continuation.ActiveTaskStore``, keyed by
   ``(tenant_id, owner_id, conversation_id)`` -- ``task.parameters``
   carries the ``dataset_id`` plus (after this fix) the currently
   selected product row. ``WorkflowPandaConversationGateway`` is the
   only component that reads/writes this store.
4. ``conversation_id`` is the single join key threading turn 1 -> turn
   2: the same value is used by ``ArtifactService.attach_to_conversation``,
   ``ActiveTaskStore.get/put``, and (per this fix)
   ``BusinessAssistantService.submit_request``.
5. On turn 2, items 1-3 above ALL still exist for this
   ``conversation_id`` -- the durable state was never lost.
6. TURN 2 COULD NOT REACH item 3 at all. The TOP-LEVEL routing decision
   (``business_assistant.intent.classify_intent`` /
   ``is_conversational``), made BEFORE any active-task lookup, decided
   turn 2's plain wording (no attachment, no explicit product-selection
   phrase matching any of the narrow ``is_explicit_*`` predicates) was
   NOT conversational. That misrouted it to the attachment-blind legacy
   ``BusinessAssistantService.execute()`` recipe-engine path, which has
   no notion of ``WorkflowPandaConversationGateway``'s
   ``ActiveTaskStore`` at all.
7. The legacy engine's fixture recipe hit a step whose capability
   dependency was never satisfiable outside the conversational pipeline
   -> ``BA_CAPABILITY_UNAVAILABLE`` / ``dependency_not_ready``.
8. Because that legacy engine's terminal state was "completed with a
   blocked, non-required step", the API layer rendered its generic
   ``ex.summary`` ("Задача выполнена. Подробности доступны в разделе
   управления.") instead of ever calling into the pipeline that could
   have answered from state.

==================================================
THE FIX (one shared boundary, no phrase-specific rules)
==================================================

``WorkflowPandaConversationGateway.has_active_product_context`` is a
new, purely STATE-based signal (not a new phrase predicate): true iff
an active ``FAMILY_EXCEL`` task with a parsed ``dataset_id`` already
exists for this ``conversation_id``. ``BusinessAssistantService.
submit_request`` now accepts ``conversation_id`` and consults this
signal, and ``business_assistant.intent.is_conversational`` uses it (as
a peer of the existing attachment-presence check, with the SAME
explicit write/publish-verb exception) to keep ANY later wording on the
SAME conversational path once a product task is already active --
regardless of which of the narrow ``is_explicit_*`` predicates the
message does or doesn't also happen to match.

Once routing is fixed, PRODUCT SELECTION itself is generalized in
``data_intel.service.execute_nl_request`` via a ``current_selection``
argument (the currently selected row's stable ``__source_row`` plus a
navigation ``history``, persisted onto the active task by
``WorkflowPandaConversationGateway._invoke_tool``): a follow-up with no
fresh SKU/EAN/model identifier of its own is resolved, via STEM-based
semantic detection (never literal sentence matching), as either
"change to a different/next product" (deterministic next-row-in-
dataset-order), "go back to the previous product" (from the persisted
history stack), or "keep showing the current product" (a plain
refinement question) -- all against the SAME already-parsed dataset,
never a fresh upload.

This test file is the mandatory production-shaped, multi-turn,
end-to-end regression proving the fix, run through the REAL
``BusinessAssistantApiService`` stack (the same object
``POST /api/v1/business-assistant/requests`` uses) with a REAL
``WorkflowPandaConversationGateway`` wired to a Bitrix bridge configured
LIVE against a mocked HTTP transport -- zero real network calls, zero
real Bitrix mutations (the recording transport raises on any
unexpected call, including ``catalog.product.add``; this test never
sends an explicit write confirmation).
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

FILENAME = "LG_TV_3PRODUCTS.xlsx"

SKU_A, EAN_A, PRICE_A, RETAIL_A = "TV-A-1001", "4600000000010", "90000", "129990"
SKU_B, EAN_B, PRICE_B, RETAIL_B = "TV-B-2002", "4600000000027", "95000", "139990"
SKU_C, EAN_C, PRICE_C, RETAIL_C = "TV-C-3003", "4600000000034", "99000", "149990"

TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# Turn 1: attachment present, explicit "take the first item" + Bitrix/Aspro
# preparation target -- selects product A deterministically.
TURN1_TEXT = (
    "Возьми первый товар из этого прайса и подготовь его для Bitrix/Aspro. "
    "Ничего не записывай в Bitrix."
)

# Turn 2: NO attachment. Semantically CHANGE_PRODUCT ("уже был" + "другой"),
# names no SKU/EAN/model and no "Bitrix" word at all -- the exact reproduced
# production shape (attachment_count == 0, no explicit-predicate match).
TURN2_TEXT = "Этот товар уже был. Возьми другой телевизор из прикреплённого прайса."

# Turn 3: NO attachment. Explicit identity (product C's own model name is a
# literal substring of this message) -- resolves via the EXISTING identifier
# matcher, never the change-product navigation.
TURN3_TEXT = "Лучше подготовь модель C."

# Turn 4: NO attachment. Plain refinement about "него" (the current
# product) -- names no fresh identifier/navigation stem at all.
TURN4_TEXT = "Покажи для него цену, EAN и точный раздел Bitrix."

# Turn 5: NO attachment. Generic "show the final write plan" ask -- no
# "Bitrix" word this time (unlike is_bitrix_write_plan_question's own
# narrower shape), exercising the new generic active-task write-plan
# branch.
TURN5_TEXT = "Покажи окончательный план записи."

# Turn 6: NO attachment. A DIFFERENT natural formulation of turn 2's
# CHANGE_PRODUCT semantic action ("следующую позицию" instead of "уже был
# ... другой") -- proves the workflow is driven by semantic stems/state,
# not one memorized sentence.
TURN6_TEXT = "Покажи следующую позицию."

WORKFLOW_DIAGNOSTIC_MARKERS = (
    "Задача выполнена",
    "Подробности доступны",
    "Requested:",
    "Findings:",
    "Fixture_mode:",
    "Waiting_approval:",
)
GENERIC_STATS_MARKER = "столбцов."


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append([SKU_A, "Модель A", "Телевизоры", "LG", EAN_A, PRICE_A, RETAIL_A])
    ws.append([SKU_B, "Модель B", "Телевизоры", "LG", EAN_B, PRICE_B, RETAIL_B])
    ws.append([SKU_C, "Модель C", "Телевизоры", "LG", EAN_C, PRICE_C, RETAIL_C])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class RoutingAndSelectionUnitTests(unittest.TestCase):
    """Isolated pins for the state-based routing signal and the
    stem/state-based (never literal-sentence) product-navigation helpers,
    including several independent paraphrases per semantic action."""

    def test_no_active_task_stays_business_without_attachment(self):
        self.assertFalse(is_conversational(TURN2_TEXT, has_attachments=False, has_active_product_task=False))
        self.assertNotEqual(
            classify_intent(TURN2_TEXT, has_attachments=False, has_active_product_task=False),
            INTENT_CONVERSATIONAL,
        )

    def test_active_task_routes_attachment_less_follow_up_conversationally(self):
        # The exact reproduced production defect: same text, no attachment,
        # but a product task IS already active for this conversation.
        self.assertTrue(is_conversational(TURN2_TEXT, has_attachments=False, has_active_product_task=True))
        self.assertEqual(
            classify_intent(TURN2_TEXT, has_attachments=False, has_active_product_task=True),
            INTENT_CONVERSATIONAL,
        )

    def test_active_task_does_not_override_explicit_write_verb(self):
        text = "Измени цену и опубликуй все товары из прайса на сайт"
        self.assertFalse(is_conversational(text, has_attachments=False, has_active_product_task=True))

    def test_change_product_paraphrases_are_recognized_generically(self):
        from data_intel.service import _wants_different_product

        paraphrases = (
            "Этот товар уже был. Возьми другой телевизор.",
            "Покажи следующую позицию.",
            "Этот уже был, выбери другой вариант.",
            "Нет, этот не нужен.",
            "Смени товар на другой.",
            "Take another one please.",
            "This is not needed, show a different product.",
        )
        for text in paraphrases:
            self.assertTrue(_wants_different_product(text), f"expected CHANGE_PRODUCT stem match: {text!r}")

    def test_previous_product_paraphrases_are_recognized_generically(self):
        from data_intel.service import _wants_previous_product

        paraphrases = (
            "Вернись к предыдущему товару.",
            "Хочу вернуться к предыдущему варианту.",
            "Go back to the previous one.",
        )
        for text in paraphrases:
            self.assertTrue(_wants_previous_product(text), f"expected PREVIOUS_PRODUCT stem match: {text!r}")

    def test_unrelated_refinement_text_is_not_a_change_product_request(self):
        from data_intel.service import _wants_different_product, _wants_previous_product

        text = TURN4_TEXT
        self.assertFalse(_wants_different_product(text))
        self.assertFalse(_wants_previous_product(text))


class ProductContextContinuitySixTurnE2ETests(unittest.IsolatedAsyncioTestCase):
    """The mandatory production-shaped 6-turn multi-turn regression, run
    through the SAME conversation end-to-end via ``BusinessAssistantApiService``,
    with a REAL ``WorkflowPandaConversationGateway`` wired to a Bitrix bridge
    configured LIVE against a mocked HTTP transport -- zero real network
    calls, zero real Bitrix mutations."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_product_context_continuity.sqlite")
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

    def _assert_not_degraded(self, summary: str, *, turn_label: str):
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(
                marker, summary, f"{turn_label} degraded into the legacy generic business workflow (found {marker!r})"
            )

    async def test_six_turn_conversation_continuity(self):
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-1"
        )

        # ---------------- TURN 1: attach XLSX, select product A ----------------
        turn1 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-1",
            idempotency_key="product-context-continuity-turn1",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)
        result1 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id)
        summary1 = result1["summary"]
        self._assert_not_degraded(summary1, turn_label="turn1")
        self.assertIn(SKU_A, summary1, "turn1 must select product A (first row)")

        # ---------------- TURN 2: NO attachment, CHANGE_PRODUCT -> B ----------------
        turn2 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN2_TEXT,
            conversation_id="conv-1",
            idempotency_key="product-context-continuity-turn2",
        )
        self.assertEqual(
            turn2.status,
            ST_COMPLETED,
            "turn2 (no attachment) must complete through the conversational pipeline, never BLOCKED by the "
            "attachment-blind legacy business workflow",
        )
        result2 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        summary2 = result2["summary"]
        self._assert_not_degraded(summary2, turn_label="turn2")
        self.assertNotIn(GENERIC_STATS_MARKER, summary2, "turn2 must not fall back to a generic spreadsheet summary")
        self.assertIn(SKU_B, summary2, "turn2 must select a DIFFERENT deterministic product (B), same XLSX/task")
        self.assertNotIn(SKU_A, summary2, "turn2 must not silently keep showing the SAME product (A) again")

        # ---------------- TURN 3: NO attachment, explicit identity -> C ----------------
        turn3 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN3_TEXT,
            conversation_id="conv-1",
            idempotency_key="product-context-continuity-turn3",
        )
        self.assertEqual(turn3.status, ST_COMPLETED)
        result3 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn3.request_id)
        summary3 = result3["summary"]
        self._assert_not_degraded(summary3, turn_label="turn3")
        self.assertIn(SKU_C, summary3, "turn3 must select product C via its explicit identity from the original XLSX")

        # ---------------- TURN 4: NO attachment, refine C (price/EAN/category) ----------------
        turn4 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN4_TEXT,
            conversation_id="conv-1",
            idempotency_key="product-context-continuity-turn4",
        )
        self.assertEqual(turn4.status, ST_COMPLETED)
        result4 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn4.request_id)
        summary4 = result4["summary"]
        self._assert_not_degraded(summary4, turn_label="turn4")
        self.assertIn(SKU_C, summary4, "turn4 must keep showing the SAME currently selected product (C)")
        self.assertIn(EAN_C, summary4)

        # ---------------- TURN 5: NO attachment, write plan for C ----------------
        turn5 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN5_TEXT,
            conversation_id="conv-1",
            idempotency_key="product-context-continuity-turn5",
        )
        self.assertEqual(turn5.status, ST_COMPLETED)
        result5 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn5.request_id)
        summary5 = result5["summary"]
        self._assert_not_degraded(summary5, turn_label="turn5")
        self.assertIn(SKU_C, summary5, "turn5's write plan must be for the currently selected product (C)")
        self.assertIn("Ничего в Bitrix не записано", summary5)

        # ---------------- TURN 6: NO attachment, paraphrased CHANGE_PRODUCT ----------------
        turn6 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN6_TEXT,
            conversation_id="conv-1",
            idempotency_key="product-context-continuity-turn6",
        )
        self.assertEqual(turn6.status, ST_COMPLETED)
        result6 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn6.request_id)
        summary6 = result6["summary"]
        self._assert_not_degraded(summary6, turn_label="turn6")
        self.assertNotIn(SKU_C, summary6, "turn6 must move away from the SAME product (C) again")

        # ZERO Bitrix mutation across all six turns: the only mocked Bitrix
        # call made anywhere in this test is the read-only
        # ``catalog.section.list`` -- never a ``catalog.product.add`` (the
        # recording transport would raise on any other unexpected call).
        methods_called = [m for m, _ in self.transport.calls]
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)


if __name__ == "__main__":
    unittest.main()
