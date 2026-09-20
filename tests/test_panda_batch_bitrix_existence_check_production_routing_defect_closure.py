"""PRODUCTION DEFECT CLOSURE (batch Bitrix existence check swallowed by the
managed-agent boundary): PR #103's ``CHECK_BITRIX_EXISTENCE_BATCH`` action
(``business_assistant.action_continuation.is_batch_bitrix_existence_check_
request`` -> ``resolve_batch_bitrix_existence_check`` ->
``WorkflowPandaConversationGateway._check_bitrix_existence_batch``) is
correct and reachable when a test constructs ``WorkflowPandaConversation
Gateway`` directly and calls ``.respond()`` on it (see
``tests/test_panda_batch_bitrix_existence_check_defect_closure.py``, which
passes). In REAL production, the SAME exact turn never reached it: Panda
answered "не могу проверить наличие в Bitrix... предоставьте экспорт"
instead.

READ-ONLY PRODUCTION TRACE (see full trace report for exhaustive detail):

  1. Top-level entry: ``POST /api/v1/business-assistant/requests`` ->
     ``business_assistant_api.router.submit_request`` ->
     ``BusinessAssistantApiService.submit()``/``submit_async()`` ->
     ``BusinessAssistantService.submit_request()`` ->
     ``business_assistant.intent.classify_intent()`` (decides
     conversational vs. legacy business-workflow) -> when conversational,
     ``BusinessAssistantService.respond_conversationally()`` ->
     ``WorkflowPandaConversationGateway.respond()``.

  2. For the exact production journey (XLSX upload -> "Проанализируй весь
     прайс." -> the batch-check turn), ``classify_intent()`` correctly
     stays conversational for ALL three turns (attachment present on turn
     1; an already-active ``FAMILY_EXCEL`` task -- established by turn 1's
     attachment ingest -- plus this turn's own explicit
     ``has_explicit_bitrix_no_write_qualifier`` on turns 2/3; see
     ``business_assistant/intent.py``'s ``is_conversational()``). So
     ``WorkflowPandaConversationGateway.respond()`` DOES run, and turn 2's
     "Проанализируй весь прайс." IS answered correctly (34 rows/4
     columns) -- exactly as reported.

  3. Root cause: inside ``respond()``, when the operator has
     ``PANDA_MANAGED_AGENT_ENABLED=true`` (the real production/Railway
     configuration this repository's own managed-agent test suite
     explicitly reproduces -- see ``tests/test_panda_managed_agent_
     integration.py``'s ``ProductionDefectSelfHealingBootstrapTests`` and
     ``tests/test_panda_managed_agent_governed_write_confirmation_defect_
     closure.py``), EVERY turn in an eligible conversation (eligibility
     is STATE-based only -- an earlier spreadsheet attachment in this
     SAME conversation, never a text/phrase check -- see
     ``managed_agent_poc.panda_bridge._is_eligible_turn``) is routed to
     ``maybe_respond_via_managed_agent()`` BEFORE ``resolve_action_turn()``
     ever runs. The managed agent exposes exactly 3 read-only tools
     (``analyze_spreadsheet``/``select_product``/``explain_bitrix_write_
     plan`` -- see that module's own docstring) and has NEVER heard of a
     batch/whole-dataset Bitrix existence check. Turn 3 (the batch-check
     text) was therefore handed to the model with no matching tool, which
     answered directly from general knowledge -- "I cannot check Bitrix
     from this chat, please provide an export" -- exactly the reported
     symptom. ``resolve_action_turn()``/``is_batch_bitrix_existence_check_
     request()``/``CHECK_BITRIX_EXISTENCE_BATCH`` were never reached at
     all for this turn.

  4. Why PR #103's own acceptance test missed this: it constructs
     ``WorkflowPandaConversationGateway`` directly and calls ``panda.
     respond()`` in-process, with ``PANDA_MANAGED_AGENT_ENABLED`` left at
     its test-process default (unset/false) -- so it NEVER exercises the
     ``if managed_agent_enabled() and ...:`` boundary this defect lives
     in, regardless of how correct the classifier/handler underneath it
     is. Passing that test therefore proves the IMPLEMENTATION is correct
     but says nothing about whether production's routing actually reaches
     it -- exactly the gap this module closes.

MINIMAL FIX (``business_assistant/conversation_gateway.py``): the managed-
agent boundary already has EXACTLY this exclusion mechanism for one other
action -- an explicit Bitrix write confirmation is gated out of the
managed agent with ``not is_explicit_bitrix_write_confirmation(text)``, so
that turn falls straight through to the EXISTING ``resolve_action_turn()``
chain unchanged (see PR #87's "PART 3" comment immediately above that
gate). The batch existence-check turn needed the SAME treatment: the
gate now also excludes ``not is_batch_bitrix_existence_check_request
(text)``, reusing the SAME EXISTING, purely textual predicate
``resolve_action_turn()`` itself already dispatches on. No new Bitrix
client, no new batch implementation, no phrase-specific response hack, no
change to the PR #103 classifier/handler at all -- only where the
managed-agent boundary decides NOT to intercept the turn in the first
place.

This module proves the fix through the REAL production routing boundary
-- ``BusinessAssistantApiService.submit()`` (the SAME object ``POST /api/
v1/business-assistant/requests`` calls), never a direct
``WorkflowPandaConversationGateway.respond()`` construction -- under BOTH
the managed-agent-off default AND the managed-agent-on (Railway-faithful)
configuration that actually swallowed the request in production.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from artifacts.service import ArtifactService
from artifacts.store import InMemoryArtifactStore
from business_assistant.action_continuation import CHECK_BITRIX_EXISTENCE_BATCH
from business_assistant.conversation_gateway import WorkflowPandaConversationGateway
from business_assistant_api.runtime import build_business_assistant_api_runtime
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from tests.test_panda_batch_bitrix_existence_check_defect_closure import (
    AMBIGUOUS_SKU,
    EXISTING_SKU,
    FILENAME,
    FORBIDDEN_ASSISTANT_PHRASES,
    MANDATORY_ACCEPTANCE_TURN_TEXT,
    NEW_SKU,
    _bitrix_bridge_and_store,
    _xlsx_bytes,
)
from tests.test_panda_managed_agent_enrichment_delegation import _make_fake_run_turn
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry

TENANT = "tenant-a"
OWNER = "user-a"
CONV = "conv-production-batch-check"

ANALYZE_TURN_TEXT = "Проанализируй весь прайс."


class _ProductionRoutingTestBase(unittest.TestCase):
    """Shared harness: a REAL ``BusinessAssistantApiService`` (the SAME
    object the ``/api/v1/business-assistant/requests`` HTTP endpoint
    calls) wired to a REAL ``WorkflowPandaConversationGateway`` and a
    REAL (fixture-mode) ``BitrixProductBridge`` -- never a direct
    gateway construction/call, and never live Bitrix."""

    def setUp(self):
        self.bridge, self.store = _bitrix_bridge_and_store()

        data_intel = DataIntelligenceService(InMemoryDatasetStore())
        self.artifact_service = ArtifactService(store=InMemoryArtifactStore())
        data_intel.artifact_service = self.artifact_service
        registry = ToolRegistry()
        register_platform_tools(registry, data_intelligence=data_intel)
        tool_gateway = ToolGateway(registry=registry, register_search=False)

        self.gateway = WorkflowPandaConversationGateway(
            workflow_engine=object(),
            run_router=object(),
            context_manager=object(),
            tool_gateway=tool_gateway,
            artifact_service=self.artifact_service,
            bitrix_product_bridge=self.bridge,
        )

        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "production_routing.sqlite")
        self.rt = build_business_assistant_api_runtime(
            db_path=self.db,
            conversation_gateway=self.gateway,
            artifact_service=self.artifact_service,
        )
        self.svc = self.rt.service

    def tearDown(self):
        self.rt.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _upload_price_list(self) -> str:
        rec = self.artifact_service.register_upload(
            tenant_id=TENANT, owner_id=OWNER, filename=FILENAME, content=_xlsx_bytes()
        )
        self.artifact_service.attach_to_conversation(
            tenant_id=TENANT, artifact_id=rec.artifact_id, conversation_id=CONV
        )
        return rec.artifact_id

    def _assert_batch_check_result(self, summary: str, *, before_catalog_size: int) -> None:
        lower = summary.casefold()
        for phrase in FORBIDDEN_ASSISTANT_PHRASES:
            self.assertNotIn(
                phrase,
                lower,
                f"production-boundary reply still contains the forbidden phrase {phrase!r}: {summary!r}",
            )
        for marker in (EXISTING_SKU, NEW_SKU, AMBIGUOUS_SKU):
            self.assertIn(marker, summary)
        self.assertEqual(
            self.gateway.last_action_decision.decision,
            CHECK_BITRIX_EXISTENCE_BATCH,
            "the turn must reach resolve_action_turn()'s CHECK_BITRIX_EXISTENCE_BATCH dispatch, "
            "not a model/legacy fallback",
        )
        self.assertEqual(
            len(self.store.catalog(TENANT)),
            before_catalog_size,
            "the batch existence check must never write to Bitrix",
        )


class ProductionBoundaryDefaultRoutingTests(_ProductionRoutingTestBase):
    """Managed agent OFF (production default) -- proves the batch check
    reaches ``CHECK_BITRIX_EXISTENCE_BATCH`` through the REAL
    ``BusinessAssistantApiService.submit()`` entry point, not only via a
    direct gateway construction."""

    def setUp(self):
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ.pop(ENABLED_ENV_VAR, None)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        if self._old_flag is not None:
            os.environ[ENABLED_ENV_VAR] = self._old_flag

    def test_batch_check_reaches_dispatch_through_real_api_service(self):
        artifact_id = self._upload_price_list()
        before_catalog_size = len(self.store.catalog(TENANT))

        turn1 = self.svc.submit(
            tenant_id=TENANT,
            owner_id=OWNER,
            message=ANALYZE_TURN_TEXT,
            artifact_refs=[artifact_id],
            conversation_id=CONV,
            idempotency_key="production-turn-1",
        )
        result1 = self.svc.get_result(tenant_id=TENANT, owner_id=OWNER, request_id=turn1.request_id)
        self.assertTrue(result1["summary"])

        turn2 = self.svc.submit(
            tenant_id=TENANT,
            owner_id=OWNER,
            message=MANDATORY_ACCEPTANCE_TURN_TEXT,
            conversation_id=CONV,
            idempotency_key="production-turn-2",
        )
        result2 = self.svc.get_result(tenant_id=TENANT, owner_id=OWNER, request_id=turn2.request_id)
        self._assert_batch_check_result(result2["summary"], before_catalog_size=before_catalog_size)


class ManagedAgentEnabledProductionRegressionTests(_ProductionRoutingTestBase):
    """PANDA_MANAGED_AGENT_ENABLED=true -- the ACTUAL production/Railway
    configuration that swallowed the request. Reproduces the exact
    3-message journey (upload -> "Проанализируй весь прайс." -> batch
    check) through the REAL ``BusinessAssistantApiService.submit()``
    entry point, with ``ManagedAgentPOC.run_turn`` mocked (never a real
    network call) to deterministically answer turn 1 the same way the
    real managed agent would (``analyze_spreadsheet``). Only ONE plan
    entry is supplied: if the batch-check turn were still (incorrectly)
    routed into the managed agent, the mock would raise ``IndexError`` on
    its second call, failing this test loudly instead of silently
    reproducing the "cannot check Bitrix" hallucination."""

    def setUp(self):
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        os.environ[ENABLED_ENV_VAR] = "true"
        super().setUp()
        os.environ["PANDA_DATA_DIR"] = self.tmp

    def tearDown(self):
        super().tearDown()
        if self._old_flag is None:
            os.environ.pop(ENABLED_ENV_VAR, None)
        else:
            os.environ[ENABLED_ENV_VAR] = self._old_flag
        if self._old_data_dir is None:
            os.environ.pop("PANDA_DATA_DIR", None)
        else:
            os.environ["PANDA_DATA_DIR"] = self._old_data_dir

    def test_batch_check_survives_managed_agent_boundary_through_real_api_service(self):
        artifact_id = self._upload_price_list()
        before_catalog_size = len(self.store.catalog(TENANT))

        # Entry [1] is deliberately the EXACT hallucinated reply the real
        # production incident reported -- it must never actually be
        # consumed once the fix is in place (the batch-check turn must be
        # excluded from the managed-agent boundary before ``run_turn`` is
        # ever called a second time). If a future regression removes that
        # exclusion, this mocked "model" reproduces the bug faithfully
        # instead of silently masking it.
        plan = [
            {
                "tool_calls": [
                    {
                        "tool": "analyze_spreadsheet",
                        "output": {"status": "ANALYZED", "row_count": 34, "column_count": 4},
                    }
                ],
                "final_output": "34 строки, 4 столбца.",
            },
            {
                "tool_calls": [],
                "final_output": (
                    "Я не могу проверить наличие товаров в Bitrix из этого чата: у меня нет "
                    "инструмента для такой проверки. Пожалуйста, предоставьте экспорт SKU."
                ),
            },
        ]

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=_make_fake_run_turn(plan)):
            turn1 = self.svc.submit(
                tenant_id=TENANT,
                owner_id=OWNER,
                message=ANALYZE_TURN_TEXT,
                artifact_refs=[artifact_id],
                conversation_id=CONV,
                idempotency_key="production-turn-1",
            )
            result1 = self.svc.get_result(tenant_id=TENANT, owner_id=OWNER, request_id=turn1.request_id)
            self.assertEqual(result1["summary"], "34 строки, 4 столбца.")

            # THE regression turn: must NOT reach the (mocked) managed
            # agent's run_turn a second time -- only one plan entry exists
            # above, so an unwanted second call raises IndexError.
            turn2 = self.svc.submit(
                tenant_id=TENANT,
                owner_id=OWNER,
                message=MANDATORY_ACCEPTANCE_TURN_TEXT,
                conversation_id=CONV,
                idempotency_key="production-turn-2",
            )
        result2 = self.svc.get_result(tenant_id=TENANT, owner_id=OWNER, request_id=turn2.request_id)
        self._assert_batch_check_result(result2["summary"], before_catalog_size=before_catalog_size)


if __name__ == "__main__":
    unittest.main()
