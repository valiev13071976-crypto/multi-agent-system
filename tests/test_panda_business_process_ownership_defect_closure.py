"""PANDA — PRODUCTION DEFECT CLOSURE: business-process ownership (retail-price
FORMULA follow-up on an already-active product/XLSX task escaping into the
attachment-blind legacy Business Workflow engine), plus the associated
false-success display defect.

Reproduced production failure (root-closure block after PR #88):

    A normal Panda chat request contained: one XLSX attachment; "prepare
    product"; "retail = purchase +7%"; "prepare for Bitrix/Aspro"; "show
    full plan"; "do NOT write yet".

    Production emitted ``business_workflow_degraded_or_empty_result`` with
    ``ba_status=COMPLETED_WITH_WARNINGS``, ``blocked_step_count=10``,
    blocked codes ``BA_CAPABILITY_UNAVAILABLE``/``dependency_not_ready``,
    and ``mapped_status=COMPLETED`` — the chat then displayed the generic
    "Задача выполнена. Подробности доступны в разделе управления."
    placeholder instead of the write plan.

ROOT CAUSE (proven from the current repository, not from hypotheses):

    ``business_assistant_api.service.BusinessAssistantApiService.submit()``
    calls ``BusinessAssistantService.submit_request()`` ->
    ``business_assistant.intent.classify_intent()`` exactly ONCE per turn,
    BEFORE any ``ActiveTask``/conversation-continuation state changes
    anything about which of the TWO real top-level execution paths (the
    conversational ``WorkflowPandaConversationGateway`` vs. the legacy
    fixture ``BusinessAssistantService.execute()`` engine) claims the
    request (``business_assistant_api/service.py``, ``submit()``/
    ``submit_async()``, ``if ba_req.intent == INTENT_CONVERSATIONAL``).

    ``classify_intent()`` -> ``is_conversational()``
    (``business_assistant/intent.py``) DOES already consult existing
    attachment presence and existing active-task continuation state
    (``has_attachments`` / ``has_active_product_task``, itself sourced
    from the SAME durable ``ActiveTaskStore`` FAMILY_EXCEL contract
    ``WorkflowPandaConversationGateway``/``resolve_action_turn`` already
    own) -- so an ownership mechanism already exists and is NOT bypassed
    in principle. It is INCOMPLETE: each of its two continuation gates
    carried a coarse "explicit write/publish verb" word list (``измени``/
    ``установ``/``опубликуй``/``publish all``) meant to let a genuine
    out-of-finger write command still escape to the legacy engine. That
    coarse list has no way to tell an actual write/publish command apart
    from an ordinary retail-price FORMULA instruction that merely shares a
    word stem with one (e.g. "Установи розничную цену как в прайсе" --
    "Установи"/"set" is establishing a pricing INPUT, not confirming a
    write) -- especially when the SAME message already explicitly says not
    to write. That misclassification is what sent the request into the
    legacy engine, which immediately blocks on Bitrix-integration
    capabilities the conversational product/pricing/Bitrix-prep flow never
    needed in the first place (``BA_CAPABILITY_UNAVAILABLE``/
    ``dependency_not_ready``), producing exactly the reported degraded
    result.

    ``COMPLETED_WITH_WARNINGS``/``PARTIALLY_COMPLETED`` are both mapped to
    the API-level ``ST_COMPLETED`` by ``_map_ba_status()``
    (``business_assistant_api/service.py``) regardless of whether any real
    work happened; combined with the frontend's ``toUserFacingSummary``/
    "Задача выполнена..." fallback (``static/shared/presentation.js``,
    gated on ``summary.status === "COMPLETED"`` in
    ``static/panda/js/app.js``), a request that only ever produced BLOCKED
    steps and ZERO findings still displayed as a normal successful
    completion.

FIX (reuses the EXISTING ownership/state mechanisms; no new store, no new
``active_lifecycle_owner`` field, no phrase matrix, no second router):

  1. ``business_assistant/action_continuation.py``: new PUBLIC helper
     ``has_explicit_bitrix_no_write_qualifier(text)`` exposes the ALREADY
     EXISTING Bitrix-target + no-write-negation signal
     (``_BITRIX_TARGET_MARKER_STEMS`` + ``_BITRIX_NO_WRITE_RE``) that
     ``is_explicit_bitrix_write_confirmation``/``is_bitrix_write_plan_
     question`` already rely on internally -- no new pattern invented,
     no duplicated logic.

  2. ``business_assistant/intent.py``: ``is_conversational()``'s TWO
     existing continuation-ownership gates (attachment-present branch AND
     active-product-task branch) now ALSO stay conversational whenever
     ``has_explicit_bitrix_no_write_qualifier`` is true for this turn's
     text, even if that same text happens to contain one of the coarse
     write-verb stems. This generalizes over the semantic FAMILY of
     "pricing/preparation instruction that explicitly defers the write" --
     not one hardcoded sentence -- while a genuine out-of-finger write
     command (no explicit Bitrix/Aspro no-write qualifier at all, e.g.
     "Измени цену и опубликуй все товары из прайса на сайт") is completely
     unaffected and still escapes to the legacy engine unchanged. The
     EXISTING durable continuation state (``ActiveTaskStore``/
     ``has_active_product_task``) is reused as-is: this fix only refines
     WHICH turns are allowed to consult that state's continuation, never
     replaces the ownership state itself.

  3. ``business_assistant_api/service.py``: ``_sync_from_execution()``'s
     existing ``business_workflow_degraded_or_empty_result`` diagnostic
     already correctly identified the exact "reported COMPLETED but had
     BLOCKED steps" shape -- it only ever LOGGED that fact. Now, when the
     mapped status is ``ST_COMPLETED`` AND at least one step is BLOCKED
     AND the execution produced literally zero ``ex.findings``, the
     mapped status is corrected to the EXISTING ``ST_BLOCKED`` terminal
     status/event (no new status invented) so the SAME frontend branch
     that already renders a non-success outcome for BLOCKED requests
     applies here too. A genuine ``COMPLETED_WITH_WARNINGS`` result that
     DID produce real findings alongside an optional/non-required blocked
     step keeps its existing ``ST_COMPLETED`` mapping unchanged --
     legitimate warning semantics are preserved, never globally redefined
     (see ``FalseSuccessMappingDefectClosureTests`` below).

This test module proves the fix at three levels:

  * ``RoutingOwnershipUnitTests`` -- direct unit coverage of the new
    ``has_explicit_bitrix_no_write_qualifier`` predicate and its effect on
    ``is_conversational``, including the negative case (genuine write
    command keeps escaping to the legacy engine unchanged).
  * ``FalseSuccessMappingDefectClosureTests`` -- direct unit coverage of
    ``_sync_from_execution``'s corrected status mapping (degraded ->
    ``ST_BLOCKED``, legitimate warnings-with-findings -> unchanged
    ``ST_COMPLETED``), plus an end-to-end reproduction of the EXACT
    production diagnostic shape (blocked steps, zero findings, mapped
    COMPLETED->BLOCKED) through the real API service.
  * ``BusinessProcessOwnershipAcceptanceTests`` -- the required end-to-end
    acceptance scenario through the REAL ``BusinessAssistantApiService``
    (the same object ``POST /api/v1/business-assistant/requests`` uses)
    with a REAL ``WorkflowPandaConversationGateway`` and a mocked Bitrix
    HTTP transport (never live Bitrix): XLSX attachment -> product
    preparation -> TWO semantically different natural-language retail-
    price/no-write follow-ups (proving this is not a single magic phrase)
    -> explicit governed confirmation reaching the EXISTING PR #88
    governed Bitrix write path exactly once.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import has_explicit_bitrix_no_write_qualifier
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant.intent import classify_intent, is_conversational
from business_assistant.models import (
    INTENT_CONVERSATIONAL,
    BusinessExecution,
    BusinessExecutionStep,
    BusinessFinding,
    STATUS_COMPLETED_WITH_WARNINGS,
)
from business_assistant_api.models import ApiRequestRecord, ST_BLOCKED, ST_COMPLETED, ST_RECEIVED
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from integrations.production.http import BoundedHttpClient
from tests.test_bitrix_live_product_create_write import _bridge_and_activation, _LiveEnv, _RecordingTransport
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

SKU = "55MRGB86B6A.ARUG"
EAN = "8806096824788"
PURCHASE_PRICE = "103198.3"
RETAIL_PRICE = "110382"
FILENAME = "LG_TV.xlsx"

# Markers that only ever appear in the LEGACY fixture business-workflow's
# diagnostic summary (business_assistant.service.BusinessAssistantService.
# _compose_summary) -- never in a conversational ConversationResult.text.
# Their presence would mean a turn degraded into the attachment-blind
# generic workflow (the exact production symptom).
WORKFLOW_DIAGNOSTIC_MARKERS = ("Requested:", "Fixture_mode:", "Waiting_approval:")
FALSE_SUCCESS_PLACEHOLDER_MARKERS = ("Задача выполнена",)

TURN1_TEXT = "Возьми первый товар из этого прайса и подготовь его для Bitrix/Aspro. Ничего не записывай в Bitrix."
# Natural-language variant #1: "Установи" (set/establish) a retail-price
# FORMULA input for the ALREADY-ACTIVE product task -- shares a word stem
# with the coarse write-verb exclusion list but is not a write command.
TURN2_TEXT = (
    "Установи розничную цену для этого товара как в прайсе. Подготовь его для Bitrix/Aspro и "
    "покажи план записи. Ничего в Bitrix пока не записывай."
)
# Natural-language variant #2: "Измени карточку" (change the card) --
# semantically different wording, different write-verb stem, same
# no-write qualifier.
TURN3_TEXT = (
    "Измени карточку товара, используя актуальную розничную цену из прайса, и подготовь её для "
    "Bitrix/Aspro. Покажи обновлённый план записи. Ничего не записывай и не публикуй в Bitrix без "
    "подтверждения."
)
TURN4_TEXT = "Подтверждаю: создай этот товар в Bitrix по показанному плану."

# A genuine out-of-finger write command (no Bitrix/Aspro no-write
# qualifier at all) must still escape to the legacy engine unchanged.
GENUINE_WRITE_COMMAND_TEXT = "Измени цену и опубликуй все товары из прайса на сайт"


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    ws.append([SKU, f"LG {SKU}", "Телевизоры", "LG", EAN, PURCHASE_PRICE, RETAIL_PRICE])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class RoutingOwnershipUnitTests(unittest.TestCase):
    """Direct unit coverage of the new no-write-qualifier override and its
    effect on ``is_conversational``/``classify_intent``."""

    def test_qualifier_detected_for_establish_stem_price_formula(self):
        self.assertTrue(has_explicit_bitrix_no_write_qualifier(TURN2_TEXT))

    def test_qualifier_detected_for_change_card_stem(self):
        self.assertTrue(has_explicit_bitrix_no_write_qualifier(TURN3_TEXT))

    def test_qualifier_absent_for_genuine_write_command(self):
        # No Bitrix/Aspro target at all in this command -- must not be
        # mistaken for a no-write qualifier.
        self.assertFalse(has_explicit_bitrix_no_write_qualifier(GENUINE_WRITE_COMMAND_TEXT))

    def test_qualifier_absent_for_bare_attachment_message(self):
        self.assertFalse(has_explicit_bitrix_no_write_qualifier("Привет, как дела?"))

    def test_active_task_turn_with_establish_stem_and_qualifier_is_conversational(self):
        self.assertTrue(is_conversational(TURN2_TEXT, has_active_product_task=True))
        self.assertEqual(
            classify_intent(TURN2_TEXT, has_active_product_task=True), INTENT_CONVERSATIONAL
        )

    def test_active_task_turn_with_change_stem_and_qualifier_is_conversational(self):
        self.assertTrue(is_conversational(TURN3_TEXT, has_active_product_task=True))
        self.assertEqual(
            classify_intent(TURN3_TEXT, has_active_product_task=True), INTENT_CONVERSATIONAL
        )

    def test_genuine_write_command_still_escapes_active_task_continuation(self):
        # No explicit Bitrix/Aspro no-write qualifier -- the existing
        # write-verb exclusion must still win, exactly as before this fix.
        self.assertFalse(is_conversational(GENUINE_WRITE_COMMAND_TEXT, has_active_product_task=True))
        self.assertNotEqual(
            classify_intent(GENUINE_WRITE_COMMAND_TEXT, has_active_product_task=True),
            INTENT_CONVERSATIONAL,
        )

    def test_attachment_branch_also_honors_the_qualifier(self):
        # Same override, attachment-presence gate (turn 1 shape) instead of
        # active-task continuation.
        self.assertTrue(is_conversational(TURN2_TEXT, has_attachments=True))
        self.assertFalse(is_conversational(GENUINE_WRITE_COMMAND_TEXT, has_attachments=True))


class FalseSuccessMappingDefectClosureTests(unittest.TestCase):
    """Direct + end-to-end coverage of the ``_sync_from_execution`` status
    correction: degraded (BLOCKED steps, zero findings) never displays as
    ``ST_COMPLETED`` again; legitimate warnings-with-findings are
    unaffected."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rt = build_business_assistant_api_runtime(
            db_path=os.path.join(self.tmp, "false_success.sqlite"), with_integration=False
        )
        self.svc = self.rt.service

    def tearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, request_id: str) -> ApiRequestRecord:
        return ApiRequestRecord(
            request_id=request_id,
            tenant_id="tenant-a",
            owner_id="user-a",
            status=ST_RECEIVED,
            message="m",
            created_at="t",
            updated_at="t",
        )

    def _degraded_execution(self, execution_id: str, findings: list) -> BusinessExecution:
        steps = {
            "s1": BusinessExecutionStep(step_id="s1", status="COMPLETED"),
            "s2": BusinessExecutionStep(step_id="s2", status="BLOCKED", error_code="BA_CAPABILITY_UNAVAILABLE"),
            "s3": BusinessExecutionStep(step_id="s3", status="BLOCKED", error_code="dependency_not_ready"),
        }
        return BusinessExecution(
            execution_id=execution_id,
            tenant_id="tenant-a",
            request_id="r",
            plan_id="p",
            plan_fingerprint="f",
            status=STATUS_COMPLETED_WITH_WARNINGS,
            steps=steps,
            findings=findings,
            summary="some summary text",
        )

    def test_degraded_result_with_zero_findings_maps_to_blocked_not_completed(self):
        rec = self._rec("r-degraded")
        self.svc._sync_from_execution(rec, self._degraded_execution("e1", []))  # noqa: SLF001
        self.assertEqual(rec.status, ST_BLOCKED)

    def test_completed_with_warnings_and_real_findings_still_maps_to_completed(self):
        rec = self._rec("r-warnings-with-findings")
        finding = BusinessFinding(finding_id="f1", kind="k", summary="real finding")
        self.svc._sync_from_execution(rec, self._degraded_execution("e2", [finding]))  # noqa: SLF001
        self.assertEqual(rec.status, ST_COMPLETED)

    def test_production_shaped_scenario_reaches_blocked_via_full_api_service(self):
        """End-to-end reproduction of the exact production diagnostic shape
        (blocked-capability legacy-engine steps, zero findings,
        ``ba_status=PARTIALLY_COMPLETED``/``COMPLETED_WITH_WARNINGS``)
        through the REAL ``BusinessAssistantApiService.submit()`` --
        proves the fix at the boundary production actually hit, not only
        in an isolated unit call."""
        with self.assertLogs("business_assistant_api.diagnostics", level="INFO") as ctx:
            rec = self.svc.submit(
                tenant_id="tenant-a",
                owner_id="user-a",
                message="Найди письмо поставщика и подготовь ответ",
                idempotency_key="degraded-shape-1",
            )
        # Before this fix: rec.status == ST_COMPLETED (the false-success
        # defect). After: the existing ST_BLOCKED terminal status/event.
        self.assertEqual(rec.status, ST_BLOCKED)
        joined = "\n".join(ctx.output)
        self.assertIn("business_workflow_degraded_or_empty_result", joined)
        result = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=rec.request_id)
        for marker in FALSE_SUCCESS_PLACEHOLDER_MARKERS:
            self.assertNotIn(marker, result.get("summary") or "")


class BusinessProcessOwnershipAcceptanceTests(unittest.TestCase):
    """REQUIRED ACCEPTANCE SCENARIO: XLSX attachment -> product preparation
    -> TWO semantically different natural-language retail-price/no-write
    follow-ups -> explicit governed confirmation, through the REAL
    ``BusinessAssistantApiService`` + REAL ``WorkflowPandaConversationGateway``
    with a mocked Bitrix HTTP transport (never live Bitrix)."""

    def setUp(self):
        self.transport = _RecordingTransport(sections=[{"id": 70, "name": "Телевизоры", "code": "televizory"}])
        self.live_env = _LiveEnv()
        self.live_env.__enter__()
        self.http_patch = mock.patch.object(BoundedHttpClient, "request", side_effect=self.transport)
        self.http_patch.start()
        self.bridge, _activation = _bridge_and_activation()

        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "ownership_acceptance.sqlite")

        data_intel = DataIntelligenceService(InMemoryDatasetStore())
        self.artifact_service = ArtifactService(store=InMemoryArtifactStore())
        data_intel.artifact_service = self.artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=data_intel)
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

    def tearDown(self):
        self.http_patch.stop()
        self.live_env.__exit__(None, None, None)
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_not_degraded(self, rec, summary: str):
        self.assertEqual(rec.status, ST_COMPLETED)
        for marker in WORKFLOW_DIAGNOSTIC_MARKERS:
            self.assertNotIn(marker, summary, f"turn degraded into the generic legacy business workflow (found {marker!r})")
        for marker in FALSE_SUCCESS_PLACEHOLDER_MARKERS:
            self.assertNotIn(marker, summary, f"turn produced the false-success placeholder (found {marker!r})")

    def test_full_acceptance_scenario(self):
        upload = self.artifact_service.register_upload(
            tenant_id="tenant-a", owner_id="user-a", filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id="tenant-a", artifact_id=upload.artifact_id, conversation_id="conv-acceptance"
        )

        # --- Turn 1: XLSX attachment enters the existing supplier-product
        # preparation process; explicit no-write. ---
        with self.assertNoLogs("business_assistant_api.diagnostics", level="INFO"):
            turn1 = self.svc.submit(
                tenant_id="tenant-a",
                owner_id="user-a",
                message=TURN1_TEXT,
                artifact_refs=[upload.artifact_id],
                conversation_id="conv-acceptance",
                idempotency_key="acceptance-turn1",
            )
        result1 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn1.request_id)
        self._assert_not_degraded(turn1, result1["summary"])
        self.assertIn(SKU, result1["summary"])
        self.assertEqual(self.transport.product_add_count, 0)

        # --- Turn 2: natural-language variant #1 ("Установи" price-formula
        # + explicit no-write). Must NOT escape into the legacy engine. ---
        with self.assertNoLogs("business_assistant_api.diagnostics", level="INFO"):
            turn2 = self.svc.submit(
                tenant_id="tenant-a",
                owner_id="user-a",
                message=TURN2_TEXT,
                conversation_id="conv-acceptance",
                idempotency_key="acceptance-turn2",
            )
        result2 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn2.request_id)
        self._assert_not_degraded(turn2, result2["summary"])
        self.assertIn(SKU, result2["summary"])
        self.assertIn(RETAIL_PRICE, result2["summary"])
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertNotIn("catalog.product.add", [m for m, _ in self.transport.calls])

        # --- Turn 3: natural-language variant #2 ("Измени карточку" +
        # explicit no-write) -- semantically different wording, proving
        # this is not a single magic phrase. ---
        with self.assertNoLogs("business_assistant_api.diagnostics", level="INFO"):
            turn3 = self.svc.submit(
                tenant_id="tenant-a",
                owner_id="user-a",
                message=TURN3_TEXT,
                conversation_id="conv-acceptance",
                idempotency_key="acceptance-turn3",
            )
        result3 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn3.request_id)
        self._assert_not_degraded(turn3, result3["summary"])
        self.assertIn(SKU, result3["summary"])
        self.assertEqual(self.transport.product_add_count, 0)
        self.assertNotIn("catalog.product.add", [m for m, _ in self.transport.calls])

        # --- Turn 4: explicit governed confirmation -- existing PR #88
        # path must still reach the governed Bitrix write exactly once. ---
        calls_before = len(self.transport.calls)
        turn4 = self.svc.submit(
            tenant_id="tenant-a",
            owner_id="user-a",
            message=TURN4_TEXT,
            conversation_id="conv-acceptance",
            idempotency_key="acceptance-turn4",
        )
        result4 = self.svc.get_result(tenant_id="tenant-a", owner_id="user-a", request_id=turn4.request_id)
        self.assertEqual(turn4.status, ST_COMPLETED)
        new_calls = [m for m, _ in self.transport.calls[calls_before:]]
        self.assertEqual(new_calls.count("catalog.product.add"), 1)
        self.assertEqual(new_calls.count("catalog.price.add"), 1)
        self.assertEqual(self.transport.product_add_count, 1)
        self.assertEqual(self.transport.offer_add_count, 0)
        self.assertEqual(self.transport.price_add_count, 1)
        self.assertIn(SKU, result4["summary"])


if __name__ == "__main__":
    unittest.main()
