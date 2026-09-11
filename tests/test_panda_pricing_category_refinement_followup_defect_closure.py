"""PANDA — PRODUCTION DEFECT CLOSURE: Turn-3 pricing/category refinement
follow-up (reproduced AFTER PR #62's single-product Bitrix prep fix)
loses the conversational product-preparation context and falls back to
the legacy fixture business-workflow's generic diagnostic reply.

Reproduced production defect (exact 3-turn scenario, continuing directly
from PR #62's closed defect):

    Turn 1: user uploads LG_TV.xlsx and asks Panda, generically, to
            prepare a product for Bitrix/Aspro (no write). Acceptable on
            its own to answer with generic spreadsheet statistics.

    Turn 2 (SAME conversation, NO new attachment): "Возьми первый товар
            из загруженного LG_TV.xlsx и подготовь его для Bitrix/Aspro.
            Ничего не записывай в Bitrix. Покажи в чате название, EAN,
            артикул, закупочную цену, розничную цену, категорию и план
            записи в Bitrix." -- CONFIRMED FIXED by PR #62: this now
            correctly returns the product card + write plan.

    Turn 3 (SAME conversation, NO new attachment, referring back to "этого
            товара" -- the product Turn 2 already selected, never
            re-selecting): "Рассчитай розничную цену для этого товара и
            определи точную категорию Bitrix/Aspro для телевизора. Покажи
            обновлённую карточку и план записи. Ничего в Bitrix пока не
            записывай." Production symptom: Panda incorrectly returned
            the legacy fixture business-workflow's generic "Задача
            выполнена. Подробности доступны в разделе управления." instead
            of the updated card + write plan.

Root cause: ``business_assistant.intent.requires_business_integration``
matches turn 3's wording too (a "покажи"/action-verb + "Bitrix"/domain-
term combination -- a DIFFERENT trigger than PR #62's own
``_BUSINESS_TASK_KEYWORDS``/"xlsx" match), so ``is_conversational`` still
routed it to the attachment-blind legacy ``BusinessAssistantService.
execute()`` recipe engine instead of ``WorkflowPandaConversationGateway``.
None of the existing narrow conversational exceptions (including PR #62's
own ``is_explicit_single_product_bitrix_prep_request``) match turn 3's
phrasing: it names no fresh single-item SELECTION signal (it refers back
to "этого товара" -- the ALREADY selected product) and it asks no "what
would be written" question about an ALREADY-enriched card.

Fix: ``business_assistant.action_continuation.
is_explicit_product_pricing_or_category_refinement_request`` (new,
narrow, additive predicate mirroring the existing ``is_explicit_single_
product_bitrix_prep_request``/``is_bitrix_write_plan_question``
precedent) recognizes this exact "(re)calculate the retail price and/or
resolve the exact Bitrix/Aspro category for the already-selected product"
shape. ``business_assistant.intent.is_conversational`` now routes it to
the conversational pipeline, and ``business_assistant.action_continuation.
resolve_action_turn`` dispatches it (at the same priority position as the
existing ``is_bitrix_write_plan_question`` branch) to a new
``resolve_product_pricing_category_refinement_request``, which:

  - reuses the SAME product Turn 2 already selected/prepared, from the
    active FAMILY_EXCEL task's persisted ``bitrix_product_fields``;
  - reuses ONLY the retail price already known/persisted (Turn 2's own
    ``bitrix_retail_price_preview``, itself sourced from the workbook's
    own "розница" column here) -- it NEVER derives/invents a new retail
    price (no such calculator exists anywhere in this codebase; see
    ``data_intel.economics``, which only computes margin/profit GIVEN an
    existing price);
  - builds the canonical write request through the EXISTING, UNCHANGED
    ``business_assistant.controlled_bitrix_write.
    build_write_request_from_fields``;
  - dispatches the EXISTING, UNCHANGED ``EXPLAIN_BITRIX_WRITE_PLAN``
    decision, so the EXISTING, UNCHANGED ``WorkflowPandaConversationGateway.
    _explain_bitrix_write_plan`` handler renders the updated card + write
    plan -- which, because a bridge and a retail price ARE present, also
    exercises the EXISTING, UNCHANGED, read-only category resolver
    (``prepare_single_product_write`` -> ``integrations.bitrix.schema.
    resolve_section_id``) end to end.

This test runs the REAL production stack end-to-end through
``BusinessAssistantApiService`` (the same object the HTTP API's
``POST /api/v1/business-assistant/requests`` handler uses) with a REAL
``WorkflowPandaConversationGateway``, wired to a Bitrix bridge configured
LIVE (mirroring ``tests/test_bitrix_live_product_create_write.py`` /
``tests/test_bitrix_complete_product_card_followup.py``'s own established,
zero-real-network pattern: ``BoundedHttpClient.request`` mocked end to
end) -- this is what actually lets the category resolver run against a
real ``catalog.section.list`` read instead of the FIXTURE bridge's
unconditional "unsupported" shortcut, while still making ZERO real network
calls and ZERO real Bitrix mutations: the recording transport raises on
any unexpected call (including ``catalog.product.add``), and this test
never sends an explicit write confirmation, so a governed create is never
even attempted for any of the three turns.
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
    is_explicit_bitrix_write_confirmation,
    is_explicit_product_pricing_or_category_refinement_request,
    is_explicit_single_product_bitrix_prep_request,
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

# Sourced from the workbook's own "розница" column (see ``_xlsx_bytes``
# below) -- this is what "the existing Panda pricing path IF ONE ALREADY
# EXISTS" resolves to: a retail price ALREADY known from the file, never
# one invented by a new calculation.
RETAIL_PRICE = "119990"

TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# Turn 1: generic upload + generic prepare-for-Bitrix ask (no specific item
# named yet) -- unaffected by either PR #62 or this fix.
TURN1_TEXT = "Проанализируй загруженный прайс и подготовь товар для Bitrix/Aspro."

# Turn 2: PR #62's own exact production request -- CLOSED, must not
# regress. Selects the first product and previews it for Bitrix/Aspro.
TURN2_TEXT = (
    "Возьми первый товар из загруженного LG_TV.xlsx и подготовь его для "
    "Bitrix/Aspro. Ничего не записывай в Bitrix. Покажи в чате название, "
    "EAN, артикул, закупочную цену, розничную цену, категорию и план "
    "записи в Bitrix."
)

# Turn 3: the EXACT reproduced production request this test closes -- SAME
# conversation, NO artifact_refs, refers back to "этого товара" (the
# product turn 2 already selected), asks Panda to (re)calculate the
# retail price and resolve the exact category, and explicitly forbids any
# write.
TURN3_TEXT = (
    "Рассчитай розничную цену для этого товара и определи точную "
    "категорию Bitrix/Aspro для телевизора. Покажи обновлённую карточку "
    "и план записи. Ничего в Bitrix пока не записывай."
)

# Markers that only ever appear in the LEGACY fixture business-workflow's
# diagnostic summary (business_assistant.service.BusinessAssistantService.
# _compose_summary) -- never in a conversational ConversationResult.text.
# Their presence would mean turn 3 degraded to the generic workflow (the
# exact reported production symptom: "Задача выполнена. Подробности
# доступны в разделе управления.").
WORKFLOW_DIAGNOSTIC_MARKERS = (
    "Задача выполнена",
    "Подробности доступны",
    "Requested:",
    "Findings:",
    "Fixture_mode:",
    "Waiting_approval:",
)

# The exact generic-spreadsheet-statistics shape (see
# ``data_intel.service.DataIntelligenceService._analyze_only_summary``)
# that turn 3 must never regress back into either.
GENERIC_STATS_MARKER = "столбцов."


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append(
        [TARGET_SKU, f"LG {TARGET_SKU}", "Телевизоры", "LG", TARGET_EAN, "103198.3", RETAIL_PRICE]
    )
    ws.append(["OTHER-SKU", "Samsung Other TV", "Телевизоры", "Samsung", "1234567890123", "50000", "65000"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class PricingCategoryRefinementRoutingUnitTests(unittest.TestCase):
    """Pins the new predicate/routing in isolation."""

    def test_turn3_text_is_explicit_pricing_or_category_refinement_request(self):
        self.assertTrue(is_explicit_product_pricing_or_category_refinement_request(TURN3_TEXT))

    def test_turn3_text_routes_conversational_without_attachment(self):
        self.assertTrue(is_conversational(TURN3_TEXT, has_attachments=False))
        self.assertEqual(classify_intent(TURN3_TEXT, has_attachments=False), INTENT_CONVERSATIONAL)

    def test_turn2_text_is_unaffected_by_the_new_predicate(self):
        # PR #62's own turn-2 predicate must keep matching turn 2, and the
        # NEW predicate must not ALSO fire for it (turn 2 names no pricing
        # calculation or category-determination verb at all).
        self.assertTrue(is_explicit_single_product_bitrix_prep_request(TURN2_TEXT))
        self.assertFalse(is_explicit_product_pricing_or_category_refinement_request(TURN2_TEXT))

    def test_bare_bitrix_mention_alone_is_unaffected(self):
        # A bare "покажи товар для Bitrix" (no pricing/category verb) must
        # NOT be force-routed by the new predicate.
        self.assertFalse(
            is_explicit_product_pricing_or_category_refinement_request("Покажи товар для Bitrix.")
        )

    def test_explicit_write_confirmation_still_wins_over_new_predicate(self):
        confirm_text = (
            "Подтверждаю: создай этот товар в Bitrix. Рассчитай окончательную "
            "розничную цену. Определи точную категорию."
        )
        self.assertTrue(is_explicit_bitrix_write_confirmation(confirm_text))
        self.assertFalse(is_explicit_product_pricing_or_category_refinement_request(confirm_text))


class PricingCategoryRefinementFollowUpDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the EXACT production 3-turn scenario end-to-end via the
    same ``BusinessAssistantApiService`` the HTTP API uses, with a REAL
    ``WorkflowPandaConversationGateway`` wired to a Bitrix bridge
    configured LIVE against a mocked HTTP transport -- zero real network
    calls, zero real Bitrix mutations (the recording transport raises on
    any unexpected call, including ``catalog.product.add``)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_pricing_category_defect.sqlite")
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

    async def test_full_reproduction_three_turns(self):
        """Turn 2 (PR #62, must not regress) + turn 3 (this fix): turn 3
        must not degrade to the legacy business workflow or generic
        spreadsheet stats, must reuse the SAME selected product, must
        resolve the exact Bitrix/Aspro category through the real (mocked)
        category resolver, must show the updated card + write plan in
        chat, and must never write to Bitrix."""
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
            idempotency_key="pricing-category-defect-turn1",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)

        # Turn 2 (SAME conversation, NO artifact_refs) -- PR #62's own
        # closed defect; pinned here unchanged so a regression in this fix
        # would also be caught in the exact same reproduction.
        turn2 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN2_TEXT,
            conversation_id="conv-1",
            idempotency_key="pricing-category-defect-turn2",
        )
        self.assertEqual(turn2.status, ST_COMPLETED)
        result2 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        summary2 = result2["summary"]
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary2, f"turn2 regressed into the generic business workflow (found {marker!r})")
        self.assertIn(TARGET_SKU, summary2)
        self.assertIn(TARGET_EAN, summary2)

        # Turn 3 (SAME conversation, NO artifact_refs, refers back to "этого
        # товара") -- the exact reproduced production defect this test
        # closes.
        turn3 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN3_TEXT,
            conversation_id="conv-1",
            idempotency_key="pricing-category-defect-turn3",
        )
        self.assertEqual(
            turn3.status,
            ST_COMPLETED,
            "turn 3 must complete through the conversational pipeline, never BLOCKED by the "
            "attachment-blind legacy business workflow",
        )
        result3 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn3.request_id)
        summary3 = result3["summary"]
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(
                marker, summary3, f"turn3 degraded into the generic business workflow (found {marker!r})"
            )
        self.assertNotIn(
            GENERIC_STATS_MARKER, summary3, "turn3 must not fall back to the generic spreadsheet summary"
        )

        # The SAME product Turn 2 already selected -- never a fresh/other
        # selection.
        self.assertIn(TARGET_SKU, summary3)

        # The retail price REUSED from Turn 2's own persisted context
        # (sourced from the workbook's "розница" column) -- never a new,
        # invented calculation.
        self.assertIn(RETAIL_PRICE, summary3)

        # The exact Bitrix/Aspro category, resolved through the REAL
        # (mocked) category resolver -- proves this reached
        # ``prepare_single_product_write`` -> ``schema.resolve_section_id``
        # end to end, not merely echoed the persisted category string back.
        self.assertIn(str(TV_SECTION_ID), summary3)

        # The write plan, rendered in chat -- never actually written.
        self.assertIn("Ничего в Bitrix не записано", summary3)

        # ZERO Bitrix mutation across all three turns: the only mocked
        # Bitrix call made anywhere in this test is the read-only
        # ``catalog.section.list`` -- never a ``catalog.product.add`` (the
        # recording transport would raise ``AssertionError`` on any other
        # unexpected call).
        methods_called = [m for m, _ in self.transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)


if __name__ == "__main__":
    unittest.main()
