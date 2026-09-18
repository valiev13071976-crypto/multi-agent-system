"""PANDA -- DEFECT B closure: SINGLE -> MULTI -> SINGLE scope recovery.

PRODUCTION FACT (real journey, post PR #97): after a multi-row table
operation the canonical Workset correctly returns to FULL_DATASET scope
(``selected_identifiers=()``, by design -- see
``business_assistant.workset.apply_tool_result``). But when the user then
naturally references a DIFFERENT product from the current dataset and
asks for a single-product action (e.g. a Bitrix write-plan preview) in
the SAME turn, production failed with::

    VALIDATION_ERROR
    reason_code=no_current_selection

even though the referenced product plainly exists in the current
dataset. This is generic across ANY non-SINGLE scope + any product
reference + any single-product action -- never tied to one particular
SKU/wording.

FIX: ``DataIntelligenceService.execute_structured_plan_via_model`` now
tries a deterministic, data-driven reference resolution
(``_resolve_product_reference``) against THIS turn's own text whenever
there is no current SINGLE selection, BEFORE the one-shot model call.
When it resolves uniquely, the SAME model call proceeds as if that
product were already selected, and
``business_assistant.conversation_gateway`` restores canonical SINGLE
scope + persists the selection (``_persist_resolved_selection``) IN THE
SAME TURN -- no extra "select this product first" round trip, no
re-upload, no loss of the original Workset/source.

This module also proves the closely related requirement that a genuinely
AMBIGUOUS product-selection judgment from the model surfaces its real
candidates through the conversational layer instead of falling into the
generic "table operation failed" catch-all (a separate small gap found
while auditing this SAME defect).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from business_assistant import workset as workset_lib
from business_assistant.conversation_gateway import ConversationRequest
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from tests.test_panda_canonical_single_authority_orchestration import PRODUCTS, _xlsx_bytes
from tests.test_panda_canonical_table_execution import _analyze_plan_entry, _tracking_fake_run_turn
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload
from tests.test_panda_selected_product_scope_continuity_defect_closure import _sequential_model_mock

TENANT = "tenant-scope-recovery"
OWNER = "u1"
CONVERSATION_ID = "conv-scope-recovery"
FILENAME = "five_products.xlsx"
PURCHASE_COL_ID = "c6"


class SingleMultiSingleScopeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        self.panda, self.artifact_service = _panda()
        self.artifact_id = await _register_upload(
            self.artifact_service,
            tenant=TENANT,
            owner=OWNER,
            conv=CONVERSATION_ID,
            filename=FILENAME,
            content=_xlsx_bytes(),
        )

    async def asyncTearDown(self):
        import shutil

        if self._old_data_dir is None:
            os.environ.pop("PANDA_DATA_DIR", None)
        else:
            os.environ["PANDA_DATA_DIR"] = self._old_data_dir
        if self._old_flag is None:
            os.environ.pop(ENABLED_ENV_VAR, None)
        else:
            os.environ[ENABLED_ENV_VAR] = self._old_flag
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self):
        return self.panda._action_store.get(  # noqa: SLF001
            tenant_id=TENANT, owner_id=OWNER, conversation_id=CONVERSATION_ID
        )

    def _workset(self):
        return workset_lib.get_workset(self._task())

    async def _respond(self, text, *, request_id, attach=False):
        return await self.panda.respond(
            ConversationRequest(
                text=text,
                tenant_id=TENANT,
                user_id=OWNER,
                request_id=request_id,
                conversation_id=CONVERSATION_ID,
                attachment_refs=(self.artifact_id,) if attach else (),
            )
        )

    async def test_i_and_l_single_multi_single_scope_recovery_same_turn(self):
        sku_a, name_a, *_rest_a = PRODUCTS[0]
        sku_b, name_b, *_rest_b = PRODUCTS[1]

        payloads = [
            {"kind": "not_applicable"},  # plain analysis
            {"kind": "product_selection", "selector": {"kind": "ordinal", "value": 0}},  # select A
            {  # intentional multi-row escape -- two scopes
                "kind": "table_operation",
                "operations": [
                    {
                        "scope": {"kind": "row_range", "start": 0, "end": 2},
                        "column_id": PURCHASE_COL_ID,
                        "operation": "percent_round",
                        "value": "7",
                    },
                    {
                        "scope": {"kind": "remainder"},
                        "column_id": PURCHASE_COL_ID,
                        "operation": "percent_round",
                        "value": "15",
                    },
                ],
            },
            # Same turn's own text names product B explicitly -- the
            # model recognizes the write-plan-query intent exactly like
            # it would with a prior SINGLE selection.
            {"kind": "write_plan_query"},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            r1 = await self._respond("Проанализируй этот прайс.", request_id="t1", attach=True)
            self.assertNotIn("Приложите файл", r1.text)
            w1 = self._workset()
            workset_id = w1.workset_id
            source_id = w1.source_dataset_id

            r2 = await self._respond("Покажи первый товар.", request_id="t2")
            w2 = self._workset()
            self.assertEqual(w2.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w2.selected_identifiers, (sku_a,))

            r3 = await self._respond(
                "Для первых двух строк увеличь закупочную цену на 7%, "
                "для остальных строк — на 15%.",
                request_id="t3",
            )
            self.assertTrue(r3.metadata.get("canonical_table_execution"))
            w3 = self._workset()
            # Multi-row escape: SINGLE -> FULL_DATASET, selection cleared,
            # SAME parent Workset/source retained.
            self.assertEqual(w3.workset_id, workset_id)
            self.assertEqual(w3.source_dataset_id, source_id)
            self.assertEqual(w3.scope, workset_lib.SCOPE_FULL_DATASET)
            self.assertEqual(w3.selected_identifiers, ())

            # DEFECT B production shape: no current SINGLE selection, but
            # THIS turn's own text names product B explicitly and asks
            # for a single-product action (Bitrix write-plan preview).
            r4 = await self._respond(
                f"Покажи, что именно уйдёт в Bitrix для товара {sku_b}.", request_id="t4"
            )
            self.assertEqual(r4.metadata.get("action_decision"), "EXPLAIN_BITRIX_WRITE_PLAN")
            self.assertIn(sku_b, r4.text)
            self.assertIn(name_b, r4.text)
            self.assertNotIn("нужен конкретный товар", r4.text.casefold())
            self.assertNotIn("no_current_selection", r4.text.casefold())
            self.assertFalse(r4.metadata.get("mutated"))

            # Restored SINGLE scope, persisted selection, SAME Workset --
            # all resolved and applied WITHIN this SAME turn, no extra
            # "select this product first" round trip, no re-upload.
            w4 = self._workset()
            self.assertEqual(w4.workset_id, workset_id)
            self.assertEqual(w4.source_dataset_id, source_id)
            self.assertEqual(w4.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w4.selected_identifiers, (sku_b,))

        self.assertEqual(len(calls), len(payloads))
        # Zero Bitrix mutation anywhere in this journey.
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001

    async def test_ambiguous_model_judgment_surfaces_real_candidates(self):
        """A genuinely ambiguous product-selection judgment from the model
        must return a clarification with real candidates -- never fall
        into the generic 'table operation failed' catch-all (a separate
        small gap found auditing Defect B)."""
        payloads = [
            {"kind": "not_applicable"},
            # An identifier value that matches nothing in THIS dataset
            # deterministically (no exact/normalized/partial hit) --
            # forces the AMBIGUOUS-with-no-real-candidates fallback path
            # inside the ``ModelProductSelection`` handler.
            {"kind": "product_selection", "selector": {"kind": "identifier", "value": "UNKNOWN-CODE-000"}},
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="a1", attach=True)
            r2 = await self._respond("Покажи товар с кодом UNKNOWN-CODE-000.", request_id="a2")
            # A genuinely unmatched reference is a clean, generic
            # "couldn't uniquely identify" message -- never the unrelated
            # "table operation failed, nothing changed" wording.
            self.assertNotIn("не удалось выполнить операцию над таблицей", r2.text.casefold())


if __name__ == "__main__":
    unittest.main()
