"""PANDA — PRODUCTION DEFECT CLOSURE (post PR #64): a single-product
Bitrix/Aspro prep request whose source XLSX row carries NO retail/selling
price column at all (only ``purchase_price``) incorrectly hides the EAN
and the exact resolved Bitrix/Aspro category too, instead of surfacing
every fact that IS read-only resolvable and only failing closed on the
genuinely unknown retail price.

Reproduced production defect (exact 2-turn scenario):

    Turn 1: user attaches LG_TV.xlsx (row has NO "розница"/selling-price
            column -- only "purchase_price") and asks Panda, in ONE
            message, to take the first product, calculate the retail
            price, resolve the exact Bitrix/Aspro category, show EAN/
            brand/purchase price, and produce a read-only Bitrix write
            plan.

            Production symptom: Panda correctly selects the first
            product (PR #64's same-turn attachment fix still works) but
            the rendered card shows ONLY name/SKU/brand/purchase price.
            It never shows EAN, never shows the exact Bitrix/Aspro
            category, and the write-plan section says the preview is
            "unavailable (no retail price or integration not configured)"
            -- even though the category IS resolvable read-only and the
            EAN IS already known, entirely independent of the price.

    Turn 2 (SAME conversation, NO new attachment, same product): "Для
            этого же товара рассчитай розничную цену, определи точную
            категорию Bitrix/Aspro и покажи EAN. Затем покажи
            обновлённую полную карточку и план записи в Bitrix. Ничего
            не записывай и не публикуй в Bitrix." Product identity is
            preserved (distinguishing this from the PR #62/#63/#64
            routing defects), but the card is still missing the same
            fields for the same reason.

Root cause (proven via ``prepare_single_product_write`` /
``WorkflowPandaConversationGateway._explain_bitrix_write_plan`` /
``format_write_plan_text`` reading + a production-shaped repro script,
see the accompanying diagnostic report -- NOT a routing regression, NOT
a duplicate Telegram/Excel pipeline):

  1. ``_explain_bitrix_write_plan`` only ever called the EXISTING,
     read-only ``prepare_single_product_write`` (the ONLY place that
     invokes ``integrations.bitrix.schema.resolve_section_id``) when
     ``write_request.retail_price`` was truthy. Whenever the source row
     had no retail price at all, this governed, read-only category
     lookup was never even attempted -- ``resolve_section_id`` was
     unreachable, not merely "not shown".
  2. Even inside ``prepare_single_product_write`` itself, category/
     section resolution was ordered AFTER the retail-price validation,
     which returns early (``STATUS_UNRESOLVED``,
     ``reason="missing_or_invalid_retail_price"``) the moment the price
     is missing -- so even a caller that removed guard #1 alone would
     still never reach category resolution.
  3. ``format_write_plan_text``'s top card section never rendered an
     "EAN: ..." line at all (unlike "Бренд"/"Закупочная цена"/"Розничная
     цена", which PR #64 already renders) -- EAN's value was only ever
     surfaced indirectly, buried inside a "НЕ будет записано: ean = ..."
     line, itself dependent on ``write_preview`` being non-empty (i.e.
     also gated behind #1/#2 above).

None of this is a routing/predicate defect: PR #62/#63/#64's own
predicates and resolvers dispatch this exact request correctly (the
product identity is preserved end to end, confirmed by
``TARGET_SKU``/``TARGET_EAN`` reasoning below) -- the defect is purely in
how the ALREADY-correctly-dispatched ``EXPLAIN_BITRIX_WRITE_PLAN`` handler
computed and rendered the preview. No new/duplicate product-preparation,
pricing, or category-resolution logic exists anywhere (confirmed:
Telegram Market Intelligence's ``market_intel.catalog_adapter`` reuses
the SAME existing catalog/matcher and reads -- never derives -- a price,
via its own explicit Phase-1 boundary; and no module anywhere computes a
NEW retail price from a bare purchase price).

Fix (all three ADDITIVE, no reordering of anything Bitrix-write-execution
related, no new pipeline):

  - ``WorkflowPandaConversationGateway._explain_bitrix_write_plan`` now
    always calls the EXISTING ``prepare_single_product_write`` whenever a
    bridge is configured, regardless of whether ``retail_price`` is
    already known.
  - ``prepare_single_product_write`` now resolves the category/section
    (the SAME existing, read-only ``schema.resolve_section_id`` lookup,
    simply reordered to run BEFORE the retail-price check, since category
    identity does not depend on price) and echoes it back (plus the
    already-known EAN) even on its unchanged
    ``missing_or_invalid_retail_price`` early return -- the retail-price
    contract itself (status/reason) is UNCHANGED; only additional,
    already-computed, read-only fields are now included instead of
    discarded.
  - ``format_write_plan_text`` now renders an "EAN: ..." line in the top
    card section (mirroring the existing "Бренд"/"Закупочная цена"
    lines) and renders the resolved category ID whenever present,
    regardless of the preview's overall status.

This test runs the REAL production stack end-to-end through
``BusinessAssistantApiService`` with a REAL
``WorkflowPandaConversationGateway``, wired to a Bitrix bridge configured
LIVE against a mocked HTTP transport (mirrors PR #62/#63/#64's own
established, zero-real-network pattern) -- so the real category resolver
(``schema.resolve_section_id``) runs end to end, and the recording
transport raises on any unexpected call (including
``catalog.product.add``), guaranteeing ZERO real Bitrix mutation. The
workbook used here deliberately carries NO retail/selling-price column
at all (only ``purchase_price``) -- this pins the "price is read, never
derived" boundary: this test must NEVER see an invented retail price
appear anywhere, only the EAN and category that ARE read-only
resolvable independent of price.
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
PURCHASE_PRICE = "103198.3"

TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# Turn 1: the EXACT reproduced production request -- ONE message, XLSX
# attached to THIS SAME turn, row has NO retail/selling price column.
TURN1_TEXT = (
    "Возьми первый товар из загруженного LG_TV.xlsx и подготовь его для "
    "Bitrix/Aspro. Рассчитай розничную цену, определи точную категорию "
    "Bitrix/Aspro и покажи EAN, бренд, закупочную цену, а также план "
    "записи в Bitrix. Ничего не записывай и не публикуй в Bitrix без "
    "моего отдельного подтверждения."
)

# Turn 2: the EXACT reproduced production follow-up -- SAME conversation,
# NO new attachment, refers back to "этого же товара".
TURN2_TEXT = (
    "Для этого же товара рассчитай розничную цену, определи точную "
    "категорию Bitrix/Aspro и покажи EAN. Затем покажи обновлённую "
    "полную карточку и план записи в Bitrix. Ничего не записывай и не "
    "публикуй в Bitrix."
)

# The exact vague fallback message this defect must no longer produce --
# a real, read-only category resolution attempt is now always made.
STALE_UNAVAILABLE_MARKER = "Предпросмотр записи Bitrix недоступен"

# Markers that only ever appear in the LEGACY fixture business-workflow's
# generic diagnostic summary -- never in a conversational
# ConversationResult.text.
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
    """Deliberately NO "розница"/selling-price column -- only
    purchase_price -- to reproduce the exact reported production defect
    (an XLSX row with genuinely no retail-price source at all)."""
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price"])
    ws.append([TARGET_SKU, f"LG {TARGET_SKU}", "Телевизоры", "LG", TARGET_EAN, PURCHASE_PRICE])
    ws.append(["OTHER-SKU", "Samsung Other TV", "Телевизоры", "Samsung", "1234567890123", "50000"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class RoutingUnitTests(unittest.TestCase):
    """Pins that this is NOT a routing/predicate defect -- both turns are
    already dispatched correctly by the EXISTING PR #62/#63/#64
    predicates/resolvers."""

    def test_turn1_matches_single_product_bitrix_prep_predicate(self):
        self.assertTrue(is_explicit_single_product_bitrix_prep_request(TURN1_TEXT))

    def test_turn2_matches_pricing_category_refinement_predicate(self):
        self.assertTrue(is_explicit_product_pricing_or_category_refinement_request(TURN2_TEXT))

    def test_both_turns_route_conversational(self):
        self.assertTrue(is_conversational(TURN1_TEXT, has_attachments=True))
        self.assertEqual(classify_intent(TURN1_TEXT, has_attachments=True), INTENT_CONVERSATIONAL)
        self.assertTrue(is_conversational(TURN2_TEXT, has_attachments=False))
        self.assertEqual(classify_intent(TURN2_TEXT, has_attachments=False), INTENT_CONVERSATIONAL)


class EanCategoryHiddenByMissingPriceDefectClosureTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the EXACT 2-turn production scenario end-to-end via the
    same ``BusinessAssistantApiService`` the HTTP API uses, with a REAL
    ``WorkflowPandaConversationGateway`` wired to a Bitrix bridge
    configured LIVE against a mocked HTTP transport -- zero real network
    calls, zero real Bitrix mutations (the recording transport raises on
    any unexpected call, including ``catalog.product.add``)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_ean_category_missing_price_defect.sqlite")
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

    async def test_turn1_shows_ean_and_exact_category_despite_missing_retail_price(self):
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-missing-price"
        )

        req = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-missing-price",
            idempotency_key="ean-category-missing-price-turn1",
        )
        self.assertEqual(req.status, ST_COMPLETED)
        result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=req.request_id)
        summary = result["summary"]

        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary, f"degraded into the generic business workflow (found {marker!r})")
        self.assertNotIn(GENERIC_STATS_MARKER, summary, "must not fall back to generic spreadsheet statistics")

        # Product identity + already-known fields (worked before the fix
        # too -- pinned so this test also catches any regression there).
        self.assertIn(TARGET_SKU, summary)
        self.assertIn("LG", summary)
        self.assertIn(PURCHASE_PRICE, summary)

        # The two fields this defect actually hides -- both must now be
        # present even though the retail price is genuinely unknown.
        self.assertIn(TARGET_EAN, summary, "EAN must be shown even when retail price is unknown")
        self.assertIn(
            str(TV_SECTION_ID),
            summary,
            "the exact Bitrix/Aspro category must resolve even when retail price is unknown",
        )

        # The stale, uninformative "preview unavailable" fallback must no
        # longer appear -- a real (mocked) category resolution attempt is
        # now always made.
        self.assertNotIn(STALE_UNAVAILABLE_MARKER, summary)

        # "Price is read, never derived": no retail price exists in the
        # source row, so none must ever be invented/shown as one.
        self.assertNotIn("Розничная цена:", summary)

        self.assertIn("Ничего в Bitrix не записано", summary)

        # ZERO Bitrix mutation: only the read-only section lookup ran.
        methods_called = [m for m, _ in self.transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)

    async def test_turn2_refinement_preserves_product_and_still_shows_ean_and_category(self):
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-missing-price-2"
        )

        turn1 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN1_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-missing-price-2",
            idempotency_key="ean-category-missing-price-2turn-1",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)

        turn2 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN2_TEXT,
            conversation_id="conv-missing-price-2",
            idempotency_key="ean-category-missing-price-2turn-2",
        )
        self.assertEqual(
            turn2.status,
            ST_COMPLETED,
            "turn 2 must complete through the conversational pipeline, product context preserved",
        )
        result2 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        summary2 = result2["summary"]

        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary2, f"turn2 degraded into the generic business workflow (found {marker!r})")

        # Product identity preserved across turns -- never a fresh/other
        # selection (distinguishes this from the PR #62/#63/#64 routing
        # defects, which this reproduction confirms are NOT regressed).
        self.assertIn(TARGET_SKU, summary2)
        self.assertIn(TARGET_EAN, summary2)
        self.assertIn(str(TV_SECTION_ID), summary2)
        self.assertNotIn(STALE_UNAVAILABLE_MARKER, summary2)
        self.assertNotIn("Розничная цена:", summary2)
        self.assertIn("Ничего в Bitrix не записано", summary2)

        methods_called = [m for m, _ in self.transport.calls]
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)


if __name__ == "__main__":
    unittest.main()
