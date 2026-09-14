"""PANDA — PRODUCTION DEFECT CLOSURE: unbounded recursion in
``business_assistant.action_continuation.resolve_action_turn`` when a
single message satisfies BOTH the product-enrichment dispatch predicate
AND the pricing/category-refinement dispatch predicate at once.

Reproduced production failure (Railway traceback):

    A brand-new Panda chat, LG_TV.xlsx attached, with this ONE message:

    "Подготовь один телевизор из этого прайса для Bitrix/Aspro по полной
    карточке товара. Заполни всё, что можешь определить и подготовить:
    название, символьный код, артикул, EAN, бренд, закупочную и
    розничную цену, правильный раздел каталога, основное изображение,
    галерею, анонс, подробное описание и характеристики именно для
    телевизора. Используй существующий процесс Product Enrichment и
    существующие правила Panda для цены и Bitrix/Aspro. Ничего пока не
    записывай в Bitrix и не публикуй. Покажи мне итоговую подготовленную
    карточку и отдельно укажи только те поля, для которых действительно
    нет подтверждённого места записи в Bitrix/Aspro."

    -> ``resolve_action_turn`` recurses without bound:

       resolve_action_turn(_skip_enrichment_dispatch=False, _skip_pricing_category_dispatch=False)
       -> is_explicit_product_enrichment_request matches, needs-excel-first
       -> resolve_action_turn(_skip_enrichment_dispatch=True, _skip_pricing_category_dispatch=False)  [BUG: other flag not propagated]
       -> is_explicit_product_pricing_or_category_refinement_request ALSO matches (same message), needs-excel-first
       -> resolve_action_turn(_skip_enrichment_dispatch=False, _skip_pricing_category_dispatch=True)  [BUG: other flag reset]
       -> is_explicit_product_enrichment_request matches AGAIN -> ... forever

    Railway hits its logging rate limit (500 logs/sec) and drops thousands
    of messages; the request never completes (``RecursionError`` /
    stack-overflow-shaped failure).

Root cause: the message names BOTH an enrichment-shaped instruction
("Подготовь ... по полной карточке товара", "Используй существующий
процесс Product Enrichment") AND a pricing/category-refinement-shaped
instruction ("определить ... правильный раздел каталога" satisfies
``_CATEGORY_DETERMINE_VERB_STEMS``/``_CATEGORY_NOUN_STEMS``) in the SAME
turn, with an XLSX attached and no ``ActiveTask`` yet (brand-new
conversation). Each of the two "excel-first" recursive branches in
``resolve_action_turn`` (~L1945 and ~L2015 pre-fix) only ever set THEIR
OWN private recursion guard (``_skip_enrichment_dispatch`` /
``_skip_pricing_category_dispatch``) on the recursive call, leaving the
OTHER guard at its default ``False``. Because this is a pure, synchronous
resolver -- the actual Excel ingestion happens later via a dispatched
tool call, never inside this function -- ``active`` never changes across
the recursive calls, so BOTH excel-ingestion-first checks keep
re-triggering each other's branch indefinitely: guard A silences branch
A on the immediate recursive call, but that call re-enters branch B
(unsilenced), whose OWN recursive call re-enters branch A (unsilenced
again), and so on without bound.

Fix: both recursive "resolve this turn as an ordinary FAMILY_EXCEL turn"
calls in ``resolve_action_turn`` now pass BOTH
``_skip_enrichment_dispatch=True`` AND
``_skip_pricing_category_dispatch=True`` -- once either branch decides to
fall through to plain Excel ingestion first, no explicit-instruction
dispatch branch may fire again on the immediate recursive call, regardless
of which one triggered it. This terminates the recursion in exactly one
hop. No phrase/regex/stem routing was added, no enrichment/pricing/
category business logic changed, and the PR #77 SIMPLE_PRODUCT
write-contract behavior is untouched (verified below).

This test runs the REAL production stack end-to-end through
``BusinessAssistantApiService`` with a REAL
``WorkflowPandaConversationGateway``, wired to a Bitrix bridge configured
LIVE against a mocked HTTP transport (same zero-real-network,
zero-real-mutation pattern as
``tests/test_panda_one_turn_upload_pricing_category_defect_closure.py``)
-- the recording transport raises on any unexpected call (including
``catalog.product.add``/``catalog.product.offer.add``/``catalog.price.add``),
guaranteeing ZERO Bitrix mutation.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import (
    ActiveTaskStore,
    is_explicit_product_enrichment_request,
    is_explicit_product_pricing_or_category_refinement_request,
    resolve_action_turn,
)
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
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

TARGET_SKU = "100MRGB96B6.ARUG"
TARGET_EAN = "8806096824788"
FILENAME = "LG_TV.xlsx"

PURCHASE_PRICE = "103198.3"
RETAIL_PRICE = "119990"

TV_SECTION_ID = 70
TV_SECTION = {"id": TV_SECTION_ID, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# The EXACT reported production request, verbatim -- used below ONLY for
# the two isolated proofs (predicate-overlap + bounded-recursion) that pin
# down the recursion defect itself, independent of anything else in the
# stack. Matches BOTH ``is_explicit_product_enrichment_request``
# ("Подготовь ... по полной карточке товара", "Используй существующий
# процесс Product Enrichment") AND
# ``is_explicit_product_pricing_or_category_refinement_request``
# ("определить ... правильный раздел каталога") at once -- this dual
# match is the exact recursion trigger reported in the Railway traceback.
REPORTED_PRODUCTION_TEXT = (
    "Подготовь один телевизор из этого прайса для Bitrix/Aspro по полной карточке товара. "
    "Заполни всё, что можешь определить и подготовить: название, символьный код, артикул, "
    "EAN, бренд, закупочную и розничную цену, правильный раздел каталога, основное "
    "изображение, галерею, анонс, подробное описание и характеристики именно для "
    "телевизора. Используй существующий процесс Product Enrichment и существующие "
    "правила Panda для цены и Bitrix/Aspro. Ничего пока не записывай в Bitrix и не "
    "публикуй. Покажи мне итоговую подготовленную карточку и отдельно укажи только те "
    "поля, для которых действительно нет подтверждённого места записи в Bitrix/Aspro."
)

# Production-shaped variant used for the full end-to-end test below. The
# literal reported sentence above names NO specific SKU/EAN/product
# identifier at all ("один телевизор из этого прайса" -- no row-selection
# signal any EXISTING, pre-existing row-lookup heuristic recognizes: it is
# not "первый товар"/"one product", and it names no SKU/EAN substring), so
# even with the recursion fixed it would fall through to the SAME
# generic, pre-existing whole-table "analyze only" summary a bare "проанализируй
# эту таблицу" request already produces today -- a SEPARATE, pre-existing
# product-selection gap this bounded task must NOT touch (task explicitly
# forbids adding phrase/regex/stem routing for this sentence). This
# variant adds ONLY the SAME "Возьми первый товар" selection phrasing the
# existing ``tests/test_panda_one_turn_upload_pricing_category_defect_
# closure.py`` already relies on (an already-working, pre-existing
# fallback -- ``_wants_first_row``/``_FIRST_PRODUCT_SELECT_RE`` in
# ``data_intel/service.py``) so the turn can be driven all the way through
# ingestion -> product selection -> Product Enrichment -> pricing/category
# -> full read-only Bitrix/Aspro plan, while still matching BOTH dispatch
# predicates in the SAME message (the exact recursion precondition) and
# preserving every semantic requirement of the reported request (full
# card, explicit enrichment process, price/category, explicit "do not
# write", explicit request for the unmapped-fields list).
PRODUCTION_TEXT = (
    "Возьми первый товар из загруженного прайса и подготовь его полную карточку товара "
    "для Bitrix/Aspro. Используй существующий процесс Product Enrichment и заполни: "
    "название, символьный код, артикул, EAN, бренд, закупочную и рассчитанную розничную "
    "цену, основное изображение, галерею, анонс, подробное описание и характеристики "
    "именно для телевизора. Определи точную категорию Bitrix/Aspro (правильный раздел "
    "каталога). Ничего пока не записывай в Bitrix и не публикуй. Покажи итоговую "
    "подготовленную карточку и перечисли поля, для которых действительно нет "
    "подтверждённого места записи в Bitrix/Aspro."
)


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append(
        [TARGET_SKU, f"LG {TARGET_SKU}", "Телевизоры", "LG", TARGET_EAN, PURCHASE_PRICE, RETAIL_PRICE]
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class DualDispatchPredicateOverlapUnitTests(unittest.TestCase):
    """Pins the exact dual-match precondition that triggers the recursion,
    in isolation from the rest of the stack, using the EXACT reported
    production sentence verbatim."""

    def test_reported_production_text_matches_both_dispatch_predicates(self):
        self.assertTrue(is_explicit_product_enrichment_request(REPORTED_PRODUCTION_TEXT))
        self.assertTrue(
            is_explicit_product_pricing_or_category_refinement_request(REPORTED_PRODUCTION_TEXT)
        )

    def test_end_to_end_variant_also_matches_both_dispatch_predicates(self):
        """The end-to-end test below uses an adapted (but still
        production-shaped) variant so the existing row-selection fallback
        can resolve a product -- confirm it still triggers the SAME dual
        dispatch match as the literal reported sentence."""
        self.assertTrue(is_explicit_product_enrichment_request(PRODUCTION_TEXT))
        self.assertTrue(is_explicit_product_pricing_or_category_refinement_request(PRODUCTION_TEXT))


class ResolveActionTurnRecursionTerminationUnitTests(unittest.TestCase):
    """Calls ``resolve_action_turn`` directly (no gateway/tool wiring) with
    a low recursion limit and the EXACT reported production sentence --
    proves the recursion is BOUNDED (terminates in a small number of hops)
    rather than merely "fast enough not to hit the default 1000 limit in
    practice"."""

    def test_resolve_action_turn_terminates_without_recursion_error(self):
        previous_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(120)
        try:
            store = ActiveTaskStore()
            try:
                result = resolve_action_turn(
                    REPORTED_PRODUCTION_TEXT,
                    tenant_id="tenant-a",
                    owner_id="user-a",
                    conversation_id="conv-recursion-unit",
                    store=store,
                    spreadsheet_attachment_count=1,
                )
            except RecursionError:
                self.fail(
                    "resolve_action_turn recursed without bound on a message that "
                    "matches both the enrichment and pricing/category dispatch predicates "
                    "-- the recursive 'excel-first' calls must propagate BOTH skip guards, "
                    "not just their own"
                )
            # Falls through to the plain FAMILY_EXCEL dispatch (no gateway
            # wired here, so no CALL_TOOL is reachable) -- the key
            # assertion is simply that a decision came back at all instead
            # of a RecursionError, AND that the enrichment chain marker
            # survived the excel-first hop.
            self.assertEqual(result.chain_to_enrichment_text, REPORTED_PRODUCTION_TEXT)
        finally:
            sys.setrecursionlimit(previous_limit)


class DualDispatchRecursionRegressionEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Reproduces the EXACT one-turn production scenario end-to-end via the
    same ``BusinessAssistantApiService`` the HTTP API uses, in a BRAND-NEW
    conversation (no prior turns), with a REAL
    ``WorkflowPandaConversationGateway`` wired to a Bitrix bridge
    configured LIVE against a mocked HTTP transport -- zero real network
    calls, zero real Bitrix mutations (the recording transport raises on
    any unexpected call)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ba_dual_dispatch.sqlite")
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

    async def test_production_shaped_first_turn_completes_without_recursion(self):
        """The ONE-TURN production request (XLSX attachment + a message
        that matches BOTH the enrichment and pricing/category dispatch
        predicates) must complete normally: recursion terminates, Product
        Enrichment is reached exactly once, the read-only Bitrix/Aspro
        plan is shown, a final response is produced, and zero Bitrix
        mutation occurs."""
        rec = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-dual-dispatch"
        )

        req = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=PRODUCTION_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-dual-dispatch",
            idempotency_key="dual-dispatch-1",
        )
        self.assertEqual(
            req.status,
            ST_COMPLETED,
            "the production-shaped first turn must complete through the conversational "
            "pipeline instead of recursing/crashing",
        )
        result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=req.request_id)
        summary = result["summary"]

        # A final response was produced and it is the prepared card, not a
        # generic failure/fallback.
        self.assertIn(TARGET_SKU, summary)
        self.assertIn(TARGET_EAN, summary)
        self.assertIn("LG", summary)

        # Product Enrichment was reached (its own combined-preview
        # rendering, ``format_combined_preview_text``, appends this exact
        # marker) -- exactly once, not zero times and not looped.
        self.assertIn(
            "Обогащение карточки — это НЕ подтверждение записи.",
            summary,
            "Product Enrichment pipeline must be reached (exactly once) for this request",
        )
        self.assertEqual(
            summary.count("Обогащение карточки — это НЕ подтверждение записи."),
            1,
            "Product Enrichment preview must be rendered exactly once, never re-entered",
        )

        # Pricing/category preparation reached (via the SAME enrichment
        # call's own read-only write preview -- NOT via a second,
        # separately re-triggered pricing/category dispatch cycle):
        # the resolved TV section id is present in the combined preview.
        self.assertIn(str(TV_SECTION_ID), summary)
        self.assertIn(RETAIL_PRICE, summary)

        # PR #77 SIMPLE_PRODUCT write-contract behavior is unchanged: a
        # non-variant TV still has NO verified article/gallery destination
        # on the (offer-free) simple product, so both are explicitly
        # reported as unmapped -- never silently written, never silently
        # dropped.
        self.assertIn("НЕ будет записано (нет проверенного назначения в Bitrix):", summary)
        self.assertIn("sku", summary)

        # ZERO Bitrix mutation: only the read-only ``catalog.section.list``
        # call is expected -- never a create/write call of any kind (the
        # recording transport would raise ``AssertionError`` on any other
        # unexpected call).
        methods_called = [m for m, _ in self.transport.calls]
        self.assertIn("catalog.section.list", methods_called)
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertNotIn("catalog.product.offer.add", methods_called)
        self.assertNotIn("catalog.price.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)


if __name__ == "__main__":
    unittest.main()
