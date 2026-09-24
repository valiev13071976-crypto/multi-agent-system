"""PANDA — PRODUCTION DEFECT CLOSURE: first-turn XLSX product-preparation
request routed to generic Excel analysis instead of the existing product-
preparation workflow.

==================================================
PROVEN PRODUCTION DEFECT
==================================================

NEW conversation. User attaches ``LG_TV.xlsx`` and, in the SAME first
turn, asks:

    "Подготовь один телевизор из этого прайса для Bitrix/Aspro. Ничего
    пока не записывай и не публикуй."

Production is healthy (PR #72's durability fix intact, no
``BA_CAPABILITY_UNAVAILABLE``/``dependency_not_ready``) -- the attachment
IS parsed and analyzed. But the reply is the generic dimension-only
spreadsheet summary ("В таблице 13 строк и 8 столбцов. Цена ... от ...
до ..., средняя ...") instead of a product card preview. The attachment
type (XLSX) was successfully ingested; the user's SEMANTIC GOAL ("prepare
ONE product for Bitrix/Aspro") was lost.

==================================================
ROOT CAUSE (proven from the code path, not guessed)
==================================================

1. This first-turn request carries an attachment, so ``business_assistant.
   intent.classify_intent`` already, correctly, routes it to
   ``INTENT_CONVERSATIONAL`` -- confirmed healthy per the production
   evidence (no capability-unavailable diagnostic). This is NOT a top-
   level routing/capability-selection defect the way #71/#72 were.
2. Inside the conversational pipeline, ``business_assistant.
   action_continuation.resolve_action_turn``'s FAMILY_EXCEL branch checks,
   IN ORDER: ``is_explicit_bitrix_write_confirmation`` (no),
   ``is_explicit_product_enrichment_request`` (requires an enrichment verb
   PLUS either "full/complete card" wording or 2+ of the enrichment
   pipeline's own component nouns -- neither present here), ``is_bitrix_
   write_plan_question`` (no), ``is_explicit_product_pricing_or_category_
   refinement_request`` (requires an explicit pricing-CALCULATION or
   category-DETERMINATION verb -- neither present here). None match, so
   the turn falls through to the generic ``data.excel_assistant`` tool
   call with the raw text.
3. ``data_intel.service.DataIntelligenceService.execute_nl_request``
   compiles the text against the deterministic nl_ops grammar (filter/
   sort/limit/percent/dedup/rename/export -- see ``data_intel/nl_ops.py``);
   this text matches none of them, so ``UnsupportedOperationError`` is
   raised and the row-selection fallback chain runs: ``_find_row_by_
   identifier`` (no SKU/EAN/model named -- no match), the CHANGE/PREVIOUS-
   product navigation branch (no active selection yet on a first turn --
   skipped), then ``_wants_first_row`` (see next point), then
   ``_wants_ordinal_row_index`` (no 2nd/3rd/... ordinal named -- no
   match). Every signal misses, so it falls all the way to
   ``_analyze_only_summary`` -- the generic dimension-only summary.
4. ``_wants_first_row``'s regex (``_FIRST_PRODUCT_SELECT_RE``) ONLY
   recognizes a narrow "choose/select/pick/take" verb (``choose|select|
   pick|выбери|возьми``, etc.) directly adjacent (within 0-3 words) to
   "first/one", OR an ordinal directly adjacent to one of FIVE generic
   nouns (``product|item|товар|позиция``). "Подготовь один телевизор..."
   uses NEITHER a recognized select verb (it uses "подготовь" -- prepare)
   NOR one of the five generic nouns (it names the concrete product-
   category noun "телевизор" -- TV) -- so it silently falls through this
   narrow signal even though it unambiguously asks for exactly one,
   unspecified, product.
5. A previously-tested request like "Возьми первый товар из этого прайса
   и подготовь его для Bitrix/Aspro." (#71's own turn 1) DOES reach
   product preparation only because its wording happens to contain
   "возьми" (a recognized select verb) immediately followed by "первый"
   -- a coincidence of phrasing, not a semantic distinction from this
   turn's request.
6. The existing product-preparation capability that SHOULD have been
   selected is the SAME one #62/#71 already use: FAMILY_EXCEL's
   ``data.excel_assistant`` tool resolving to a single product row via
   ``_row_lookup_result`` (the SAME schema-driven preview, zero new
   parsing, zero Bitrix mutation, write still requires separate
   confirmation) -- NOT a new/second workflow.

Attachment type (XLSX) never by itself decided the business action here;
the DEFECT was that the capability-selection boundary had NO general
signal at all for "the user wants exactly one, unspecified, product
prepared" beyond a handful of literal verb/noun combinations -- so this
perfectly normal paraphrase fell through to the generic-analysis default.

==================================================
THE FIX (one general signal, no phrase-specific patch)
==================================================

``data_intel.service._wants_single_unspecified_product`` (new) generalizes
via the SAME two-independent-stem-group convention this module already
uses for other semantic-navigation signals (``_wants_different_product``/
``_wants_previous_product``) instead of enumerating sentences or product-
category nouns: an explicit SINGLE-QUANTITY marker (exactly one item,
unspecified which -- any inflection of "один"/"первый"/"one"/"first")
present ANYWHERE in the message, TOGETHER with an independent "prepare
this as a product/card" signal (the "готов" root shared by EVERY
inflection of "подготовь"/"подготовка"/"готовая"/"готовый", or an explicit
mention of a product CARD, "карточк-"/"card"). Neither group alone is
sufficient, so this can never widen into "route every XLSX turn to
product preparation" -- a pure spreadsheet-statistics ask names no
single-quantity marker at all, and a bare "Подготовь товар для Bitrix"
(no specific item -- the existing, already-pinned turn-1 generic-analysis
shape in ``test_panda_single_product_bitrix_prep_followup_defect_
closure.py``) also names no single-quantity marker, so both stay
completely unaffected. Wired as an additional OR-alternative into the
SAME existing ``_wants_first_row`` call site in ``execute_nl_request``'s
fallback chain -- ``_wants_first_row`` itself, and every sentence it
already recognized, is unchanged.

This test file is the mandatory production-shaped regression, run
through the REAL ``BusinessAssistantApiService`` stack (the same object
``POST /api/v1/business-assistant/requests`` uses) with a REAL
``WorkflowPandaConversationGateway`` wired to a Bitrix bridge configured
LIVE against a mocked HTTP transport -- zero real network calls, zero
real Bitrix mutations.
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
from business_assistant.action_continuation import ActiveTaskStore, FAMILY_EXCEL
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant_api.models import ST_COMPLETED
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.service import DataIntelligenceService, _wants_single_unspecified_product
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

FILENAME = "LG_TV.xlsx"

SKU_A, EAN_A, PRICE_A, RETAIL_A = "TV-A-1001", "4600000000010", "90000", "129990"
SKU_B, EAN_B, PRICE_B, RETAIL_B = "TV-B-2002", "4600000000027", "95000", "139990"
SKU_C, EAN_C, PRICE_C, RETAIL_C = "TV-C-3003", "4600000000034", "99000", "149990"

TV_SECTION = {"id": 70, "name": "Телевизоры", "code": "televizory"}
ELECTRONICS_SECTION = {"id": 61, "name": "Электроника", "code": "elektronika"}

# The exact reproduced production request (CASE 1 / PRODUCT INTENT).
PRODUCTION_TEXT = (
    "Подготовь один телевизор из этого прайса для Bitrix/Aspro. "
    "Ничего пока не записывай и не публикуй."
)

# Mandatory positive paraphrases (CASE 3) -- test-only; production must
# not hardcode any of these sentences.
PARAPHRASE_A = "Подготовь один товар из этого файла для магазина."
PARAPHRASE_B = "Сделай карточку одного телевизора из прайса."
PARAPHRASE_C = "Возьми одну позицию и подготовь её для Bitrix."
PARAPHRASE_D = "Мне нужна готовая карточка одного товара, пока без публикации."

# Mandatory negative/contrast requests (CASE 2) -- generic Excel analysis
# must be preserved for all of these, with the SAME XLSX attachment.
ANALYSIS_TEXT_AVERAGE = "Проанализируй этот прайс и покажи среднюю цену."
ANALYSIS_TEXT_ROW_COUNT = "Сколько строк в таблице?"
ANALYSIS_TEXT_MIN_MAX = "Покажи минимальную и максимальную цену."
ANALYSIS_TEXT_SUMMARY = "Сделай сводку по этому Excel."

# Continuation check (attachment-less second turn, PR #71/#72 regression).
CONTINUATION_TEXT = "Этот товар уже был, выбери другой."

GENERIC_STATS_MARKER = "столбцов."
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
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append([SKU_A, "Модель A", "Телевизоры", "LG", EAN_A, PRICE_A, RETAIL_A])
    ws.append([SKU_B, "Модель B", "Телевизоры", "LG", EAN_B, PRICE_B, RETAIL_B])
    ws.append([SKU_C, "Модель C", "Телевизоры", "LG", EAN_C, PRICE_C, RETAIL_C])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _assert_not_degraded(case: unittest.TestCase, summary: str, *, turn_label: str) -> None:
    for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
        case.assertNotIn(
            marker, summary, f"{turn_label} degraded into the legacy generic business workflow (found {marker!r})"
        )


class SingleUnspecifiedProductSignalUnitTests(unittest.TestCase):
    """Isolated pins for the new general signal, including the mandatory
    positive paraphrases and negative/contrast requests."""

    def test_production_sentence_and_paraphrases_recognized(self):
        for text in (PRODUCTION_TEXT, PARAPHRASE_A, PARAPHRASE_B, PARAPHRASE_C, PARAPHRASE_D):
            self.assertTrue(_wants_single_unspecified_product(text), f"expected a match: {text!r}")

    def test_analysis_requests_are_not_recognized(self):
        for text in (
            ANALYSIS_TEXT_AVERAGE,
            ANALYSIS_TEXT_ROW_COUNT,
            ANALYSIS_TEXT_MIN_MAX,
            ANALYSIS_TEXT_SUMMARY,
        ):
            self.assertFalse(_wants_single_unspecified_product(text), f"expected NO match: {text!r}")

    def test_existing_bare_prepare_verb_turn1_is_unaffected(self):
        # The already-pinned turn-1 generic ask from
        # test_panda_single_product_bitrix_prep_followup_defect_closure.py
        # -- no single-quantity marker at all -- must stay unaffected.
        self.assertFalse(
            _wants_single_unspecified_product("Проанализируй загруженный прайс и подготовь товар для Bitrix/Aspro.")
        )


class XlsxFirstTurnCapabilityRoutingE2ETests(unittest.IsolatedAsyncioTestCase):
    """Production-shaped regression through the REAL API/service
    composition boundary used by ``POST /api/v1/business-assistant/
    requests``."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.transport = _RecordingTransport(sections=[TV_SECTION, ELECTRONICS_SECTION])
        self.live_env = _LiveEnv()
        self.live_env.__enter__()
        self.http_patch = patch.object(BoundedHttpClient, "request", side_effect=self.transport)
        self.http_patch.start()
        self.bridge, _activation = _bridge_and_activation()

    async def asyncTearDown(self):
        self.http_patch.stop()
        self.live_env.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_conversation_runtime(self, *, db_name: str):
        svc = DataIntelligenceService(InMemoryDatasetStore())
        artifact_service = ArtifactService(store=InMemoryArtifactStore())
        svc.artifact_service = artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=svc)
        gateway = ToolGateway(registry=registry, register_search=False)
        conversation_gateway = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=gateway,
            artifact_service=artifact_service,
            bitrix_product_bridge=self.bridge,
        )
        rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, db_name),
            conversation_gateway=conversation_gateway,
            artifact_service=artifact_service,
        )
        return rt, artifact_service, conversation_gateway

    async def test_case1_product_intent_selects_and_prepares_one_product(self):
        rt, artifact_service, conversation_gateway = self._new_conversation_runtime(db_name="case1.sqlite")
        rec = artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-product"
        )

        turn1 = rt.service.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=PRODUCTION_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-product",
            idempotency_key="xlsx-first-turn-case1-product",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)
        result1 = rt.service.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id)
        summary1 = result1["summary"]
        _assert_not_degraded(self, summary1, turn_label="case1")
        self.assertNotIn(
            GENERIC_STATS_MARKER,
            summary1,
            "product-preparation request must not return generic spreadsheet statistics as the final result",
        )
        self.assertIn(SKU_A, summary1, "exactly one product (the first row) must be selected and previewed")

        # Active product context established.
        active = conversation_gateway._action_store.get(  # noqa: SLF001
            tenant_id="tenant-a", owner_id="user-a", conversation_id="conv-product"
        )
        self.assertIsNotNone(active)
        self.assertEqual(active.family, FAMILY_EXCEL)
        self.assertTrue(str(active.parameters.get("dataset_id") or ""))
        self.assertTrue(active.parameters.get("bitrix_product_fields"), "product fields must be resolved/persisted")

        # No Bitrix mutation, no publication.
        methods_called = [m for m, _ in self.transport.calls]
        self.assertNotIn("catalog.product.add", methods_called)
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 0)

        rt.close()

    async def test_case2_excel_analysis_intent_stays_generic(self):
        for label, text in (
            ("average", ANALYSIS_TEXT_AVERAGE),
            ("row_count", ANALYSIS_TEXT_ROW_COUNT),
            ("min_max", ANALYSIS_TEXT_MIN_MAX),
            ("summary", ANALYSIS_TEXT_SUMMARY),
        ):
            with self.subTest(label=label):
                rt, artifact_service, conversation_gateway = self._new_conversation_runtime(
                    db_name=f"case2-{label}.sqlite"
                )
                rec = artifact_service.register_upload(
                    tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
                )
                artifact_service.attach_to_conversation(
                    tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id=f"conv-analysis-{label}"
                )

                turn1 = rt.service.submit(
                    tenant_id="tenant-a",
                    owner_id="user-a",
                    message=text,
                    artifact_refs=[rec.artifact_id],
                    conversation_id=f"conv-analysis-{label}",
                    idempotency_key=f"xlsx-first-turn-case2-{label}",
                )
                self.assertEqual(turn1.status, ST_COMPLETED)
                result1 = rt.service.get_result(
                    tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id
                )
                summary1 = result1["summary"]
                _assert_not_degraded(self, summary1, turn_label=f"case2-{label}")
                self.assertIn(
                    GENERIC_STATS_MARKER,
                    summary1,
                    f"a pure spreadsheet-analysis request ({text!r}) must return generic Excel analysis",
                )
                self.assertNotIn(SKU_A, summary1, "no product must be selected for a pure analysis request")

                # Product-preparation workflow NOT started: dataset tracking
                # (an ordinary, expected FAMILY_EXCEL task) may legitimately
                # exist, but no product fields/row selection were resolved.
                active = conversation_gateway._action_store.get(  # noqa: SLF001
                    tenant_id="tenant-a", owner_id="user-a", conversation_id=f"conv-analysis-{label}"
                )
                if active is not None:
                    self.assertFalse(
                        active.parameters.get("bitrix_product_fields"),
                        "no product context must be created for a pure analysis request",
                    )
                    self.assertFalse(
                        active.parameters.get("bitrix_row_selection"),
                        "no row selection must be created for a pure analysis request",
                    )

                rt.close()

    async def test_case3_paraphrases_converge_on_the_same_capability(self):
        for label, text in (
            ("paraphrase_a", PARAPHRASE_A),
            ("paraphrase_b", PARAPHRASE_B),
            ("paraphrase_c", PARAPHRASE_C),
            ("paraphrase_d", PARAPHRASE_D),
        ):
            with self.subTest(label=label):
                rt, artifact_service, conversation_gateway = self._new_conversation_runtime(
                    db_name=f"case3-{label}.sqlite"
                )
                rec = artifact_service.register_upload(
                    tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
                )
                artifact_service.attach_to_conversation(
                    tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id=f"conv-para-{label}"
                )

                turn1 = rt.service.submit(
                    tenant_id="tenant-a",
                    owner_id="user-a",
                    message=text,
                    artifact_refs=[rec.artifact_id],
                    conversation_id=f"conv-para-{label}",
                    idempotency_key=f"xlsx-first-turn-case3-{label}",
                )
                self.assertEqual(turn1.status, ST_COMPLETED)
                result1 = rt.service.get_result(
                    tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id
                )
                summary1 = result1["summary"]
                _assert_not_degraded(self, summary1, turn_label=f"case3-{label}")
                self.assertNotIn(GENERIC_STATS_MARKER, summary1, f"paraphrase must select a product: {text!r}")
                self.assertIn(SKU_A, summary1, f"paraphrase must converge on the SAME product-selection capability: {text!r}")

                rt.close()

    async def test_continuation_check_attachment_less_second_turn_reuses_context(self):
        """PR #71/#72 regression check: after CASE 1, an attachment-less
        follow-up in the SAME conversation must reuse the active product
        context (never re-upload, never the legacy degraded workflow)."""
        rt, artifact_service, conversation_gateway = self._new_conversation_runtime(db_name="continuation.sqlite")
        rec = artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=rec.artifact_id, conversation_id="conv-continuation"
        )

        turn1 = rt.service.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=PRODUCTION_TEXT,
            artifact_refs=[rec.artifact_id],
            conversation_id="conv-continuation",
            idempotency_key="xlsx-first-turn-continuation-turn1",
        )
        self.assertEqual(turn1.status, ST_COMPLETED)
        result1 = rt.service.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id)
        self.assertIn(SKU_A, result1["summary"])

        turn2 = rt.service.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=CONTINUATION_TEXT,
            conversation_id="conv-continuation",
            idempotency_key="xlsx-first-turn-continuation-turn2",
        )
        self.assertEqual(
            turn2.status,
            ST_COMPLETED,
            "attachment-less follow-up must complete through the conversational pipeline, never the legacy "
            "degraded workflow",
        )
        result2 = rt.service.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        summary2 = result2["summary"]
        _assert_not_degraded(self, summary2, turn_label="continuation-turn2")
        self.assertNotIn(GENERIC_STATS_MARKER, summary2, "continuation must not fall back to generic analysis")
        self.assertIn(SKU_B, summary2, "a DIFFERENT product must be selected from the SAME original XLSX source")
        self.assertNotIn(SKU_A, summary2, "must not silently keep showing the SAME product again")

        rt.close()


if __name__ == "__main__":
    unittest.main()
