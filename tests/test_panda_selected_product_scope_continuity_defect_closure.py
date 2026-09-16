"""PANDA -- SELECTED-PRODUCT SCOPE CONTINUITY / RETAIL-PRICE SEMANTICS /
ARTIFACT_REF FILENAME CONTRACT -- production defect closure (post PR #94).

PR #94 fixed stale/first-product selection: after uploading a price list
and asking to "show the fourth product", Panda correctly selects that
exact SKU and keeps it for one immediate price follow-up. Real production
evidence after #94 showed FIVE remaining defects this module closes:

  1. once a product is selected, its identity must stay canonical
     (SINGLE scope + selected identifiers) across a value mutation, an
     ordinary follow-up question, a Bitrix write-plan preview, and an
     explicit SKU resend -- never silently demoted to general table scope
     merely because the mutation was internally represented as a
     ``row_range`` (see ``data_intel.service.execute_structured_plan_via_
     model``'s ``selected_was_targeted`` -- now decided from the
     EXECUTOR's own ``row_changes``, never from matching scope literals);
  2. a specific-attribute question about the selected product ("what
     quantity does it have") must return the real value (or state plainly
     that the source data doesn't have it) instead of repeating the whole
     product card -- new ``kind: "field_query"`` (``data_intel.
     nl_plan_llm``);
  3. a RETAIL-price request must never mutate-then-relabel an existing
     PURCHASE-price column -- ``price_role`` (validated against each
     column's own semantic role) forces a NEW, separately-tagged retail
     column via ``add_column_percent`` instead of an in-place ``percent_
     round`` on the purchase-price column;
  4. "what would be written to Bitrix for this product" must reuse the
     CURRENTLY SELECTED product with zero repeated SKU -- new ``kind:
     "write_plan_query"``, dispatched straight into the EXISTING, unchanged
     ``resolve_bitrix_write_plan_question``/``_explain_bitrix_write_plan``;
  5. ``POST /attachments``' returned ``artifact_ref`` must never be
     rejected by ``POST /requests``' stricter character-set validation for
     an ordinary filename containing spaces/parentheses/Cyrillic --
     ``business_assistant_api.uploads.save_upload`` now mints an OPAQUE
     ``artifact://upload/{upload_id}`` ref, independent of the display
     filename.

Every model call below is deterministically mocked (a fixed JSON payload
per turn, consumed in order) -- this is a production defect-closure test,
not a live-model acceptance test, and this sandbox may have a REAL
``OPENAI_API_KEY`` injected (per-secret, unrelated to this repository's own
test fixtures), so leaving the canonical-table-execution model call
unmocked for ANY turn would risk a real network call. Managed agent stays
disabled throughout (the default) -- ``kind: "product_selection"`` and the
new field_query/write_plan_query kinds are handled entirely inside the
existing canonical model-plan boundary (PR #94), so this journey never
needs the managed-agent integration boundary at all. Zero live network,
zero live Bitrix (``self.panda._bitrix_bridge is None`` throughout).
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from openpyxl import Workbook

from business_assistant import workset as workset_lib
from business_assistant.conversation_gateway import ConversationRequest
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from tests.test_panda_canonical_table_execution import _analyze_plan_entry, _tracking_fake_run_turn
from tests.test_panda_canonical_workset_single_data_ownership import _first_dataset_store
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload

TENANT = "tenant-a"
OWNER = "u1"
CONVERSATION_ID = "conv-scope-continuity"
FILENAME = "scope_continuity.xlsx"

SKU_X, NAME_X, CATEGORY_X, BRAND_X, EAN_X, QTY_X, PURCHASE_X = (
    "TV-X-1001", "Телевизор X", "Телевизоры", "LG", "4600000000010", "5", "22251.80",
)
SKU_Y, NAME_Y, CATEGORY_Y, BRAND_Y, EAN_Y, QTY_Y, PURCHASE_Y = (
    "TV-Y-2002", "Телевизор Y", "Телевизоры", "LG", "4600000000027", "12", "18000.00",
)
SKU_Z, NAME_Z, CATEGORY_Z, BRAND_Z, EAN_Z, QTY_Z, PURCHASE_Z = (
    "TV-Z-3003", "Телевизор Z", "Телевизоры", "LG", "4600000000034", "7", "9990.00",
)

# Column order below drives the model-facing stable ids (see
# data_intel.nl_plan_llm._column_ids): c0=sku, c1=product_name,
# c2=category, c3=brand, c4=ean, c5=quantity, c6=purchase_price. This
# fixture deliberately has ONLY ONE price column (classified
# ROLE_PURCHASE_PRICE by data_intel.mapping's own "purchase_price" alias)
# -- no separate retail/selling-price column -- exactly the production
# shape defect 3 was reported against.
SKU_COL_ID = "c0"
QUANTITY_COL_ID = "c5"
PURCHASE_COL_ID = "c6"


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "quantity", "purchase_price"])
    ws.append([SKU_X, NAME_X, CATEGORY_X, BRAND_X, EAN_X, QTY_X, PURCHASE_X])
    ws.append([SKU_Y, NAME_Y, CATEGORY_Y, BRAND_Y, EAN_Y, QTY_Y, PURCHASE_Y])
    ws.append([SKU_Z, NAME_Z, CATEGORY_Z, BRAND_Z, EAN_Z, QTY_Z, PURCHASE_Z])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sequential_model_mock(payloads: list[dict]):
    """Deterministic replacement for the EXISTING one-shot model seam
    (``agents.openai_agent.OpenAIAgent.run``, see ``data_intel.nl_plan_llm.
    _default_model_call``) -- pops ONE fixed JSON payload per call, in
    order, and fails loudly (an ``AssertionError``, never a silent extra
    real network call) if more calls happen than this test explicitly
    accounted for. Mirrors ``tests.test_panda_canonical_table_execution.
    _mock_model_json`` but supports an ordered SEQUENCE of distinct
    responses for a single multi-turn journey."""
    from agents.provider_result import ProviderResult

    remaining = list(payloads)
    calls: list[str] = []

    async def _fake_run(self, prompt):
        calls.append(prompt)
        if not remaining:
            raise AssertionError(
                f"unexpected extra canonical-table-execution model call (prompt={prompt[:200]!r}); "
                "no more mocked payloads were queued for this deterministic journey"
            )
        payload = remaining.pop(0)
        return ProviderResult(text=json.dumps(payload), provider_id="openai", model_id="test-model")

    env_patch = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model"})
    model_patch = mock.patch("agents.openai_agent.OpenAIAgent.run", new=_fake_run)
    return env_patch, model_patch, calls


class SelectedProductScopeContinuityJourneyTests(unittest.IsolatedAsyncioTestCase):
    """The full mandatory acceptance journey: ingest -> select product B
    (Y) -> retail-price mutation -> ordinary follow-up -> Bitrix write-plan
    preview -> explicit SKU resend -> explicit multi-row escape."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ["PANDA_DATA_DIR"] = self.tmp
        # The canonical-table-execution boundary (data_intel.nl_plan_llm/
        # ``_maybe_execute_canonical_table_operation``) is only reached
        # from ``WorkflowPandaConversationGateway.respond`` from INSIDE the
        # managed-agent-eligible branch (see conversation_gateway.py's own
        # ``if managed_agent_enabled() and not is_explicit_bitrix_write_
        # confirmation(text):`` gate) -- so this journey needs the flag on
        # even though, thanks to PR #94's product_selection/field_query/
        # write_plan_query kinds, the managed agent's OWN run_turn is never
        # actually invoked for any turn here except the plain "analyze"
        # turn (a genuine kind: not_applicable judgment, mocked below).
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

    async def test_full_selected_product_scope_continuity_journey(self):
        payloads = [
            # Turn 1: plain analysis of a freshly-attached spreadsheet --
            # genuinely not a table operation.
            {"kind": "not_applicable"},
            # Turn 2: "take the second product" -- ordinal product
            # selection, resolved entirely inside the model-plan boundary
            # (PR #94), never touching the managed agent.
            {"kind": "product_selection", "selector": {"kind": "ordinal", "value": 1}},
            # Turn 3: "add 6.8% to the RETAIL price and show the card" --
            # only a PURCHASE-price column exists, so a genuine retail
            # request must derive a NEW, separately-tagged column rather
            # than mutating purchase_price in place (defect 3).
            {
                "kind": "table_operation",
                "operations": [
                    {
                        "scope": {"kind": "selected"},
                        "column_id": PURCHASE_COL_ID,
                        "operation": "add_column_percent",
                        "value": "6.8",
                        "new_column": "retail_price",
                        "price_role": "retail",
                    }
                ],
            },
            # Turn 4a: an ordinary follow-up about the SAME selected
            # product's quantity -- must answer the real value, not
            # repeat the whole card (defect 2).
            {"kind": "field_query", "column_id": QUANTITY_COL_ID, "field_label": "количество"},
            # Turn 4b: a follow-up about an attribute the table genuinely
            # does not have -- must say so plainly, never invent a value.
            {"kind": "field_query", "column_id": None, "field_label": "цвет корпуса"},
            # Turn 5: "show exactly what will be written to Bitrix after
            # confirmation" for the SAME selected product (defect 4).
            {"kind": "write_plan_query"},
            # Turn 6 (explicit SKU resend) intentionally has NO queued
            # payload: the deterministic identifier short-circuit (see
            # data_intel.service._deterministic_identifier_row) must
            # resolve it WITHOUT ever calling the model.
            # Turn 7: an EXPLICIT multi-row request -- intentionally
            # escapes SINGLE scope back to the whole table.
            {
                "kind": "table_operation",
                "operations": [
                    {
                        "scope": {"kind": "all"},
                        "column_id": PURCHASE_COL_ID,
                        "operation": "percent_round",
                        "value": "5",
                    }
                ],
            },
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        # Turn 1 alone gets a genuine ``kind: not_applicable`` verdict, so
        # (with managed agent enabled per asyncSetUp) it falls through to
        # the managed agent's own read-only ``analyze_spreadsheet`` tool --
        # faked here exactly like ``test_panda_canonical_table_execution``
        # does, never a real subprocess/model call.
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            # Turn 1: ingest.
            r1 = await self._respond("Проанализируй этот прайс.", request_id="t1", attach=True)
            self.assertNotIn("Приложите файл", r1.text)
            w1 = self._workset()
            self.assertIsNotNone(w1)
            workset_id = w1.workset_id
            source_id = w1.source_dataset_id
            self.assertEqual(w1.scope, workset_lib.SCOPE_FULL_DATASET)

            self.assertEqual(len(run_turn_calls), 1)

            # Turn 2: select product B (Y) -- canonical Workset SINGLE.
            r2 = await self._respond("Возьми второй товар из этого прайса.", request_id="t2")
            self.assertEqual(r2.metadata.get("action_decision"), "SELECT_CANONICAL_PRODUCT")
            # The managed agent is NEVER entered again for the rest of
            # this journey -- every remaining turn is resolved entirely
            # inside the canonical model-plan boundary.
            self.assertEqual(len(run_turn_calls), 1)
            w2 = self._workset()
            self.assertEqual(w2.workset_id, workset_id)
            self.assertEqual(w2.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w2.selected_identifiers, (SKU_Y,))

            # Turn 3: retail-price mutation for the SAME selected product.
            r3 = await self._respond(
                "Прибавь 6,8% к розничной цене и покажи карточку.", request_id="t3"
            )
            self.assertTrue(r3.metadata.get("canonical_table_execution"))
            w3 = self._workset()
            # DEFECT 1: SINGLE scope + selected identity survive a mutation
            # that was internally executed as a row_range.
            self.assertEqual(w3.workset_id, workset_id)
            self.assertEqual(w3.source_dataset_id, source_id)
            self.assertEqual(w3.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w3.selected_identifiers, (SKU_Y,))
            dataset_after_mutation = w3.current_dataset_id
            self.assertNotEqual(dataset_after_mutation, source_id)

            preview3 = r3.metadata.get("table_operation_preview") or {}
            changed3 = preview3.get("changed_rows") or []
            self.assertEqual(len(changed3), 1)
            self.assertEqual(changed3[0]["column"], "retail_price")
            self.assertEqual(changed3[0]["source_value"], PURCHASE_Y)
            expected_retail_y = str(round(float(PURCHASE_Y) * 1.068, 2))
            self.assertAlmostEqual(float(changed3[0]["resulting_value"]), float(expected_retail_y), places=2)
            # Never relabeled: purchase price and retail price are BOTH
            # present in the resulting card/task state, distinctly.
            self.assertIn(f"Закупочная цена (из файла): {PURCHASE_Y}", r3.text)
            self.assertIn("Розничная цена", r3.text)
            self.assertNotIn(f"Закупочная цена (из файла): {expected_retail_y}", r3.text)

            store = _first_dataset_store(self.panda)
            rows_after_mutation = store.get_rows(dataset_after_mutation, tenant_id=TENANT)
            self.assertEqual(len(rows_after_mutation), 3)
            row_y = next(r for r in rows_after_mutation if r.get("sku") == SKU_Y)
            row_x = next(r for r in rows_after_mutation if r.get("sku") == SKU_X)
            row_z = next(r for r in rows_after_mutation if r.get("sku") == SKU_Z)
            # DEFECT 3 core invariant: purchase_price is UNCHANGED for
            # every row, including the mutated one.
            self.assertEqual(row_x.get("purchase_price"), PURCHASE_X)
            self.assertEqual(row_y.get("purchase_price"), PURCHASE_Y)
            self.assertEqual(row_z.get("purchase_price"), PURCHASE_Z)
            # Only the SELECTED row (Y) got a derived retail_price; the
            # other two rows were never touched by this SINGLE-scoped op.
            self.assertAlmostEqual(float(row_y.get("retail_price")), float(expected_retail_y), places=2)
            self.assertIn(row_x.get("retail_price"), (None, ""))
            self.assertIn(row_z.get("retail_price"), (None, ""))

            bitrix_fields_after_mutation = dict(self._task().parameters.get("bitrix_product_fields") or {})
            self.assertEqual(bitrix_fields_after_mutation.get("sku"), SKU_Y)
            self.assertEqual(bitrix_fields_after_mutation.get("purchase_price"), PURCHASE_Y)
            retail_preview_after_mutation = str(self._task().parameters.get("bitrix_retail_price_preview") or "")
            self.assertAlmostEqual(float(retail_preview_after_mutation), float(expected_retail_y), places=2)

            # Turn 4a: DEFECT 2 -- a specific-attribute follow-up about the
            # SAME selected product returns the real value, not the card.
            r4a = await self._respond("Какое количество у этого товара?", request_id="t4a")
            self.assertEqual(r4a.metadata.get("action_decision"), "FIELD_QUERY")
            self.assertIn(QTY_Y, r4a.text)
            self.assertNotIn("Карточка товара", r4a.text)
            w4a = self._workset()
            self.assertEqual(w4a.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w4a.selected_identifiers, (SKU_Y,))

            # Turn 4b: a field genuinely absent from the source data.
            r4b = await self._respond("Какой у него цвет корпуса?", request_id="t4b")
            self.assertEqual(r4b.metadata.get("action_decision"), "FIELD_QUERY")
            self.assertIn("нет значения", r4b.text.casefold())
            self.assertNotIn("Карточка товара", r4b.text)

            # Turn 5: DEFECT 4 -- Bitrix write-plan preview for the SAME
            # selected product, with zero repeated SKU and zero "I need a
            # concrete product" answer.
            r5 = await self._respond(
                "Покажи, что именно будет после подтверждения загружено в Bitrix.",
                request_id="t5",
            )
            self.assertEqual(r5.metadata.get("action_decision"), "EXPLAIN_BITRIX_WRITE_PLAN")
            self.assertIn(SKU_Y, r5.text)
            self.assertIn(NAME_Y, r5.text)
            self.assertIn(f"Закупочная цена: {PURCHASE_Y}", r5.text)
            self.assertIn("Розничная цена:", r5.text)
            self.assertNotIn("нужен конкретный товар", r5.text.casefold())
            self.assertFalse(r5.metadata.get("mutated"))
            self.assertFalse((r5.metadata.get("bitrix_write_result") or {}))
            w5 = self._workset()
            self.assertEqual(w5.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w5.selected_identifiers, (SKU_Y,))

            # Turn 6: DEFECT 1.F -- resending the EXACT SKU must resolve
            # deterministically, never fall into the table-operation
            # failure path, and never call the model at all.
            calls_before_resend = len(calls)
            r6 = await self._respond(SKU_Y, request_id="t6")
            self.assertEqual(len(calls), calls_before_resend)  # zero extra model calls
            self.assertNotIn("не удалось выполнить операцию над таблицей", r6.text.casefold())
            self.assertEqual(r6.metadata.get("action_decision"), "SELECT_CANONICAL_PRODUCT")
            w6 = self._workset()
            self.assertEqual(w6.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w6.selected_identifiers, (SKU_Y,))

            # Turn 7: an EXPLICIT multi-row request intentionally escapes
            # SINGLE scope -- never inferred merely from an internal
            # row_range representation, only from an explicit "all" scope.
            r7 = await self._respond(
                "Увеличь закупочную цену на 5% для всех товаров и покажи результат.",
                request_id="t7",
            )
            self.assertTrue(r7.metadata.get("canonical_table_execution"))
            w7 = self._workset()
            self.assertEqual(w7.workset_id, workset_id)
            self.assertEqual(w7.source_dataset_id, source_id)
            self.assertEqual(w7.scope, workset_lib.SCOPE_FULL_DATASET)
            self.assertEqual(w7.selected_identifiers, ())
            preview7 = r7.metadata.get("table_operation_preview") or {}
            self.assertEqual(len(preview7.get("changed_rows") or []), 3)

        # Every queued payload was consumed exactly once, in order -- no
        # more, no fewer -- and the original spreadsheet was never
        # mutated/lost across the whole journey.
        self.assertEqual(len(calls), len(payloads))
        self.assertEqual(len(run_turn_calls), 1)
        original_rows = store.get_rows(source_id, tenant_id=TENANT)
        self.assertEqual(len(original_rows), 3)

        # Zero Bitrix mutation anywhere in this journey.
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001

    async def test_retail_price_request_never_mutates_purchase_price_in_place(self):
        """Focused defect-3 regression: if the model INCORRECTLY tried to
        mutate the existing purchase-price column in place for a request
        explicitly tagged ``price_role: "retail"``, this must fail closed
        (a clear unresolved state) rather than silently corrupt/relabel
        purchase price -- proving the validation guard in ``data_intel.
        nl_plan_llm._validate_operation`` is actually wired up end to end,
        not just unit-tested in isolation."""
        payloads = [
            {"kind": "not_applicable"},
            {"kind": "product_selection", "selector": {"kind": "ordinal", "value": 0}},
            {
                "kind": "table_operation",
                "operations": [
                    {
                        "scope": {"kind": "selected"},
                        "column_id": PURCHASE_COL_ID,
                        "operation": "percent_round",
                        "value": "6.8",
                        "price_role": "retail",
                    }
                ],
            },
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])
        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self._respond("Проанализируй этот прайс.", request_id="g1", attach=True)
            await self._respond("Возьми первый товар из этого прайса.", request_id="g2")
            before = self._workset()
            r3 = await self._respond("Увеличь розничную цену на 6.8%.", request_id="g3")

        self.assertFalse(r3.metadata.get("canonical_table_execution"))
        self.assertTrue(r3.metadata.get("table_operation_failed_closed"))
        self.assertEqual(r3.metadata.get("table_operation_failure_reason"), "price_role_mismatch")
        # Zero side effects: purchase price / Workset are exactly as
        # turn 2 left them -- nothing was silently corrupted.
        after = self._workset()
        self.assertEqual(after.current_dataset_id, before.current_dataset_id)
        self.assertEqual(after.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(after.selected_identifiers, (SKU_X,))
        store = _first_dataset_store(self.panda)
        rows = store.get_rows(after.current_dataset_id, tenant_id=TENANT)
        row_x = next(r for r in rows if r.get("sku") == SKU_X)
        self.assertEqual(row_x.get("purchase_price"), PURCHASE_X)


class ArtifactRefFilenameContractTests(unittest.TestCase):
    """DEFECT 5: ``POST /attachments``' ``artifact_ref`` must never be
    rejected by ``POST /requests``'s stricter validation for an ordinary
    display filename -- direct unit coverage of the exact two-boundary
    contract (``business_assistant_api.uploads.save_upload`` producing the
    ref, ``business_assistant_api.normalizer.normalize_submission``
    validating it), independent of the full HTTP app/conversation-gateway
    stack."""

    def test_filenames_with_spaces_parens_and_cyrillic_survive_upload_then_request(self):
        from business_assistant_api.normalizer import normalize_submission
        from business_assistant_api.uploads import save_upload

        production_filenames = [
            "TCL (1).xlsx",
            "Прайс TCL сентябрь.xlsx",
            "Прайс сентябрь (финал).xlsx",
        ]
        for filename in production_filenames:
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as tmp:
                    upload = save_upload(
                        base_dir=tmp,
                        tenant_id=TENANT,
                        owner_id=OWNER,
                        filename=filename,
                        content=b"PK\x03\x04fake-xlsx-body",
                        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                    ref = upload["artifact_ref"]
                    self.assertTrue(ref)
                    # POST /requests must accept this ref -- never
                    # artifact_ref_invalid for a normal user filename.
                    submission = normalize_submission(
                        message="Проанализируй загруженный прайс.",
                        artifact_refs=[ref],
                    )
                    self.assertEqual(submission.artifact_refs, (ref,))
                    # The user-visible filename is preserved for display,
                    # completely independent of the (opaque) ref's
                    # validity -- the user is never required to rename.
                    self.assertTrue(upload["filename"])

    def test_artifact_ref_is_opaque_and_independent_of_display_filename(self):
        from business_assistant_api.uploads import save_upload

        with tempfile.TemporaryDirectory() as tmp:
            upload_a = save_upload(
                base_dir=tmp,
                tenant_id=TENANT,
                owner_id=OWNER,
                filename="Прайс сентябрь (финал).xlsx",
                content=b"same-bytes",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            # The ref never embeds the raw filename -- exactly the
            # ambiguity that let an unsafe character slip into a
            # supposedly-opaque identifier.
            self.assertNotIn("Прайс", upload_a["artifact_ref"])
            self.assertNotIn("(", upload_a["artifact_ref"])
            self.assertNotIn(")", upload_a["artifact_ref"])
            self.assertNotIn(" ", upload_a["artifact_ref"])
            self.assertEqual(upload_a["artifact_ref"], f"artifact://upload/{upload_a['upload_id']}")


if __name__ == "__main__":
    unittest.main()
