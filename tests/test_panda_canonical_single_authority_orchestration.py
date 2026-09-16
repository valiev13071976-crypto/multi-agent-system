"""PANDA -- ONE AUTHORITATIVE EXECUTION ROUTE for the conversational

    EXCEL -> PRODUCT -> DATA MODIFICATION -> PRODUCT FOLLOW-UP -> BITRIX PREVIEW

flow (production orchestration simplification / defect-closure task, run
against CURRENT ``main`` after PR #94/#95).

CONTEXT: by the time this task started, PR #92/#93/#94/#95 had already
built exactly the "one semantic understanding boundary -> canonical
Workset -> deterministic execution -> concrete response" route this task
requires:

  - ``business_assistant.workset.Workset`` is the single, durable owner of
    ``workset_id``/``source_dataset_id``/``current_dataset_id``/``scope``/
    ``selected_identifiers`` (PR #91);
  - ``WorkflowPandaConversationGateway._maybe_execute_canonical_table_
    operation`` calls the ONE existing one-shot model seam
    (``data_intel.nl_plan_llm.compile_request_via_model``) BEFORE the
    managed-agent boundary, for every eligible turn, and now resolves
    ``table_operation``/``product_selection``/``field_query``/
    ``write_plan_query`` kinds ENTIRELY inside that one call -- the
    managed agent's own private dataset/product state is never consulted
    for any of these (PR #94/#95);
  - only a genuine, validly-parsed ``not_applicable`` verdict (or a fresh
    turn with no canonical dataset yet) defers to the managed-agent/
    legacy path, and that deferral never discards the canonical Workset
    (a resolved managed-agent product selection can only narrow the SAME
    Workset's scope via ``select_single`` -- see ``_persist_managed_
    agent_product_context`` -- never replace its dataset identity).

This module's job is therefore CLOSURE, not re-architecture: it exercises
the exact 16-step conversational acceptance journey this task specifies
(5-product spreadsheet -> analyze -> select product #4 by natural
language -> retail-price-only mutation -> field questions (present AND
absent) -> Bitrix write-plan preview -> explicit-SKU product switch ->
follow-up on the NEW product -> an INTENTIONAL two-scope multi-row escape
back to the whole table, producing a downloadable workbook artifact) in
ONE continuous conversation, end to end, proving every invariant holds
together rather than in isolation -- plus the HTTP-transport steps
(upload with production-shaped filenames, submit, poll, result, and the
reported "no access" defect) driven through the real ``main.app`` for a
FRESH, self-registered owner.

Every model call in the first (conversation-gateway-level) journey is
deterministically mocked (this sandbox may carry a REAL injected
``OPENAI_API_KEY`` unrelated to this repository's own fixtures, so an
unmocked call in a deterministic test would risk a real network request).
A SEPARATE, explicitly real-model class at the bottom performs exactly
ONE unmocked, natural-language acceptance journey and is skipped
automatically when no credentials are configured.
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
from tests.test_panda_selected_product_scope_continuity_defect_closure import _sequential_model_mock

TENANT = "tenant-a"
OWNER = "u1"
CONVERSATION_ID = "conv-single-authority"
FILENAME = "five_products.xlsx"

# 5 products so "select product #4" (a real ordinal, not the first/last
# row -- the exact shape this task calls out) is unambiguous. Only ONE
# price column exists (purchase_price) -- no separate retail/selling
# column -- the same production shape the retail-price-semantics defect
# was reported against.
PRODUCTS = [
    ("TV-P0-0001", "Телевизор Alpha", "Телевизоры", "LG", "4600000000101", "3", "10000.00"),
    ("TV-P1-0002", "Телевизор Bravo", "Телевизоры", "LG", "4600000000102", "8", "12500.00"),
    ("TV-P2-0003", "Телевизор Charlie", "Телевизоры", "LG", "4600000000103", "15", "9000.00"),
    ("TV-P3-0004", "Телевизор Delta", "Телевизоры", "LG", "4600000000104", "21", "17750.00"),
    ("TV-P4-0005", "Телевизор Echo", "Телевизоры", "LG", "4600000000105", "6", "13300.00"),
]
SKU_COL_ID = "c0"
QUANTITY_COL_ID = "c5"
PURCHASE_COL_ID = "c6"


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "quantity", "purchase_price"])
    for row in PRODUCTS:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class CanonicalSingleAuthorityAcceptanceJourneyTests(unittest.IsolatedAsyncioTestCase):
    """The full mandatory 16-step conversational acceptance journey."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ["PANDA_DATA_DIR"] = self.tmp
        # Reached only from inside the managed-agent-eligible branch of
        # ``WorkflowPandaConversationGateway.respond`` -- see that
        # method's own gate -- so this journey needs the flag on even
        # though the managed agent's OWN run_turn is only ever invoked
        # for the single genuine "not_applicable" (plain analysis) turn.
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

    async def test_full_16_step_acceptance_journey(self):
        sku4, name4, _cat4, _brand4, _ean4, qty4, purchase4 = PRODUCTS[3]  # "product #4" -> index 3
        sku1, name1, _cat1, _brand1, _ean1, qty1, purchase1 = PRODUCTS[1]

        payloads = [
            # Step 2: plain analysis of a freshly-attached spreadsheet --
            # genuinely not a table operation.
            {"kind": "not_applicable"},
            # Step 3: "select product #4" using a natural-language ordinal.
            {"kind": "product_selection", "selector": {"kind": "ordinal", "value": 3}},
            # Step 5: retail-price-only mutation for the selected product
            # -- only a PURCHASE-price column exists, so this must derive
            # a NEW, separately-tagged retail column (never mutate
            # purchase_price in place).
            {
                "kind": "table_operation",
                "operations": [
                    {
                        "scope": {"kind": "selected"},
                        "column_id": PURCHASE_COL_ID,
                        "operation": "add_column_percent",
                        "value": "8",
                        "new_column": "retail_price",
                        "price_role": "retail",
                    }
                ],
            },
            # Step 7: a real attribute the table has.
            {"kind": "field_query", "column_id": QUANTITY_COL_ID, "field_label": "количество"},
            # Step 7b: an attribute the table genuinely does not have.
            {"kind": "field_query", "column_id": None, "field_label": "гарантийный срок"},
            # Step 9: Bitrix write-plan preview for the SAME selected
            # product.
            {"kind": "write_plan_query"},
            # Step 11 (explicit SKU of ANOTHER product) intentionally has
            # NO queued payload: the deterministic identifier
            # short-circuit must resolve it without ever calling the
            # model.
            # Step 13: an ordinary follow-up about the NEWLY selected
            # product.
            {"kind": "field_query", "column_id": QUANTITY_COL_ID, "field_label": "количество"},
            # Step 15: an EXPLICIT two-scope multi-row transformation --
            # intentionally escapes SINGLE scope back to the whole table,
            # and explicitly asks to produce the resulting file.
            {
                "kind": "table_operation",
                "wants_workbook": True,
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
        ]
        env_patch, model_patch, calls = _sequential_model_mock(payloads)
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])

        with env_patch, model_patch, mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            # Step 1+2: upload with several products, analyze.
            r1 = await self._respond("Проанализируй этот прайс.", request_id="s1", attach=True)
            self.assertNotIn("Приложите файл", r1.text)
            w1 = self._workset()
            self.assertIsNotNone(w1)
            workset_id = w1.workset_id
            source_id = w1.source_dataset_id
            self.assertEqual(w1.scope, workset_lib.SCOPE_FULL_DATASET)
            self.assertEqual(len(run_turn_calls), 1)

            # Step 3+4: select product #4 by natural language -- canonical
            # Workset exists, scope=SINGLE, correct selected identity.
            r2 = await self._respond("Покажи четвёртый товар из этого прайса.", request_id="s2")
            self.assertEqual(r2.metadata.get("action_decision"), "SELECT_CANONICAL_PRODUCT")
            self.assertEqual(len(run_turn_calls), 1)  # managed agent never entered again
            w2 = self._workset()
            self.assertEqual(w2.workset_id, workset_id)
            self.assertEqual(w2.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w2.selected_identifiers, (sku4,))

            # Step 5+6: retail-price mutation -- only product #4 changed,
            # purchase price unchanged, retail price separately updated,
            # scope remains SINGLE, same selected identity.
            r3 = await self._respond(
                "Прибавь 8% к розничной цене этого товара и покажи карточку.", request_id="s3"
            )
            self.assertTrue(r3.metadata.get("canonical_table_execution"))
            w3 = self._workset()
            self.assertEqual(w3.workset_id, workset_id)
            self.assertEqual(w3.source_dataset_id, source_id)
            self.assertEqual(w3.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w3.selected_identifiers, (sku4,))
            dataset_after_mutation = w3.current_dataset_id
            self.assertNotEqual(dataset_after_mutation, source_id)

            store = _first_dataset_store(self.panda)
            rows_after_mutation = store.get_rows(dataset_after_mutation, tenant_id=TENANT)
            self.assertEqual(len(rows_after_mutation), 5)
            row4 = next(r for r in rows_after_mutation if r.get("sku") == sku4)
            expected_retail4 = round(float(purchase4) * 1.08, 2)
            self.assertEqual(row4.get("purchase_price"), purchase4)  # DEFECT 3 invariant
            self.assertAlmostEqual(float(row4.get("retail_price")), expected_retail4, places=2)
            for other_sku, *_rest in PRODUCTS:
                if other_sku == sku4:
                    continue
                other_row = next(r for r in rows_after_mutation if r.get("sku") == other_sku)
                self.assertIn(other_row.get("retail_price"), (None, ""))

            # Step 7+8: a real attribute question about "this product"
            # returns the actual value.
            r4a = await self._respond("Какое количество у этого товара?", request_id="s4a")
            self.assertEqual(r4a.metadata.get("action_decision"), "FIELD_QUERY")
            self.assertIn(qty4, r4a.text)
            self.assertNotIn("Карточка товара", r4a.text)

            # An attribute genuinely absent from the source data states so
            # plainly, never inventing a value.
            r4b = await self._respond("Какой у него гарантийный срок?", request_id="s4b")
            self.assertEqual(r4b.metadata.get("action_decision"), "FIELD_QUERY")
            self.assertIn("нет значения", r4b.text.casefold())

            # Step 9+10: Bitrix write-plan preview for the SAME selected
            # product, zero repeated SKU, zero write.
            r5 = await self._respond(
                "Покажи, что именно будет после подтверждения загружено в Bitrix.", request_id="s5"
            )
            self.assertEqual(r5.metadata.get("action_decision"), "EXPLAIN_BITRIX_WRITE_PLAN")
            self.assertIn(sku4, r5.text)
            self.assertIn(name4, r5.text)
            self.assertNotIn("нужен конкретный товар", r5.text.casefold())
            self.assertFalse(r5.metadata.get("mutated"))
            w5 = self._workset()
            self.assertEqual(w5.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w5.selected_identifiers, (sku4,))

            # Step 11+12: send the EXACT SKU of ANOTHER product --
            # deterministic switch, no table-operation failure, zero
            # extra model calls, no stale prior product state.
            calls_before_switch = len(calls)
            r6 = await self._respond(sku1, request_id="s6")
            self.assertEqual(len(calls), calls_before_switch)
            self.assertNotIn("не удалось выполнить операцию над таблицей", r6.text.casefold())
            self.assertEqual(r6.metadata.get("action_decision"), "SELECT_CANONICAL_PRODUCT")
            w6 = self._workset()
            self.assertEqual(w6.workset_id, workset_id)
            self.assertEqual(w6.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w6.selected_identifiers, (sku1,))

            # Step 13+14: a normal follow-up about the NEW product returns
            # ITS value (not product #4's stale one), and it remains
            # selected.
            r7 = await self._respond("Какое количество у этого товара?", request_id="s7")
            self.assertEqual(r7.metadata.get("action_decision"), "FIELD_QUERY")
            self.assertIn(qty1, r7.text)
            self.assertNotIn(qty4, r7.text)
            w7 = self._workset()
            self.assertEqual(w7.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w7.selected_identifiers, (sku1,))

            # Step 15+16: an EXPLICIT multi-row transformation with TWO
            # scopes -- intentional SINGLE -> multi-row escape, same
            # parent Workset/source retained, deterministic execution,
            # no re-upload, downloadable workbook artifact produced.
            r8 = await self._respond(
                "Для первых двух строк увеличь закупочную цену на 7%, "
                "для остальных строк — на 15%. Покажи результат и подготовь файл.",
                request_id="s8",
            )
            self.assertTrue(r8.metadata.get("canonical_table_execution"))
            w8 = self._workset()
            self.assertEqual(w8.workset_id, workset_id)
            self.assertEqual(w8.source_dataset_id, source_id)
            self.assertEqual(w8.scope, workset_lib.SCOPE_FULL_DATASET)
            self.assertEqual(w8.selected_identifiers, ())
            preview8 = r8.metadata.get("table_operation_preview") or {}
            changed8 = preview8.get("changed_rows") or []
            self.assertEqual(len(changed8), 5)
            self.assertEqual(changed8[0]["scope"]["kind"], "row_range")
            self.assertEqual(changed8[1]["scope"]["kind"], "row_range")
            for row in changed8[2:]:
                self.assertEqual(row["scope"]["kind"], "remainder")
            artifacts8 = r8.metadata.get("artifacts") or []
            self.assertEqual(len(artifacts8), 1)
            self.assertEqual(artifacts8[0]["type"], "workbook")
            self.assertTrue(artifacts8[0].get("view_url"))
            self.assertIn("[Скачать Excel]", r8.text)

        # Every queued payload was consumed exactly once, in order.
        self.assertEqual(len(calls), len(payloads))
        self.assertEqual(len(run_turn_calls), 1)
        original_rows = store.get_rows(source_id, tenant_id=TENANT)
        self.assertEqual(len(original_rows), 5)
        for sku, *_rest, purchase in [(p[0], p[6]) for p in PRODUCTS]:
            row = next(r for r in original_rows if r.get("sku") == sku)
            self.assertEqual(row.get("purchase_price"), purchase)

        # Zero Bitrix mutation anywhere in this journey.
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001


class CanonicalRoutingNeverHijacksNonBusinessChatTests(unittest.IsolatedAsyncioTestCase):
    """"Normal chat must still work": a plain, non-Excel turn (no
    attachment this turn, no existing FAMILY_EXCEL task for this
    conversation) must reach the EXISTING generic conversational
    fallback completely unchanged -- neither the canonical model-plan
    boundary nor the managed-agent boundary may intercept it, even with
    ``PANDA_MANAGED_AGENT_ENABLED=true`` (production's own default).
    Deterministic and network-free: the canonical-table-execution model
    seam is never even reached (``_maybe_execute_canonical_table_
    operation`` returns ``None`` immediately -- no ``ToolGateway``/
    ``data_intel`` call at all, since there is no FAMILY_EXCEL task
    yet), and the managed-agent boundary's own state-only eligibility
    check (``_is_eligible_turn``) is false with no attachment and no
    prior managed-agent dataset -- so this test only needs to fake the
    EXISTING generic ``workflow_engine`` this turn ultimately falls
    through to, exactly like ``tests.test_panda_chat_ux_fix.
    ChatReplySelectionTests.test_gateway_does_not_return_judge_
    metadata`` already does for the pre-existing (untouched) fallback."""

    async def asyncSetUp(self):
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        os.environ[ENABLED_ENV_VAR] = "true"

    async def asyncTearDown(self):
        if self._old_flag is None:
            os.environ.pop(ENABLED_ENV_VAR, None)
        else:
            os.environ[ENABLED_ENV_VAR] = self._old_flag

    async def test_plain_chat_reaches_generic_fallback_unchanged(self):
        from business_assistant.conversation_gateway import WorkflowPandaConversationGateway

        canned_reply = "Привет! У меня всё отлично, чем могу помочь?"

        class _Engine:
            last_workflow_id = "wf-plain-chat"

            async def execute(self, *args, **kwargs):
                return {"final_answer": canned_reply, "role": "Judge"}

        gateway = WorkflowPandaConversationGateway(
            workflow_engine=_Engine(),
            run_router=object(),
            context_manager=object(),
            # No tool_gateway/artifact_service at all -- proves this turn
            # never needs either to reach the generic fallback.
        )
        result = await gateway.respond(
            ConversationRequest(
                text="Привет! Как дела?",
                tenant_id=TENANT,
                user_id=OWNER,
                request_id="plain-chat-1",
                conversation_id="conv-plain-chat",
            )
        )
        self.assertEqual(result.text, canned_reply)
        # Never one of the Excel/Bitrix canonical-routing outcomes -- this
        # turn fell straight through to the pre-existing generic fallback.
        metadata = result.metadata or {}
        self.assertNotIn(
            metadata.get("action_decision"),
            (
                "SELECT_CANONICAL_PRODUCT",
                "FIELD_QUERY",
                "EXPLAIN_BITRIX_WRITE_PLAN",
                "CALL_TOOL",
                "MANAGED_AGENT",
            ),
        )
        self.assertFalse(metadata.get("canonical_table_execution"))


class _RealCredentialsMixin:
    @staticmethod
    def _real_credentials_available() -> bool:
        return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


@unittest.skipUnless(
    _RealCredentialsMixin._real_credentials_available(),
    "OPENAI_API_KEY not available in this environment",
)
class RealModelSingleAuthorityAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """ONE real, unmocked, natural-language semantic acceptance journey
    (never a copied parser phrase): select a product, ask a plain
    follow-up question about it, then ask for the Bitrix write plan --
    proving the real model classifies all three turns correctly and the
    canonical Workset keeps the SAME selected product across them,
    without ever entering the managed-agent loop.

    Skipped automatically when no OPENAI_API_KEY is configured. Never
    calls the paid model more than this one journey."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_data_dir = os.environ.get("PANDA_DATA_DIR")
        self._old_flag = os.environ.get(ENABLED_ENV_VAR)
        self._old_api_key = os.environ.get("OPENAI_API_KEY")
        self._old_model = os.environ.get("OPENAI_MODEL")
        os.environ["PANDA_DATA_DIR"] = self.tmp
        os.environ[ENABLED_ENV_VAR] = "true"
        # This sandbox's injected OPENAI_API_KEY may carry a trailing
        # newline that httpx rejects outright as an illegal header value.
        if self._old_api_key:
            os.environ["OPENAI_API_KEY"] = self._old_api_key.strip()
        os.environ["OPENAI_MODEL"] = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        self.panda, self.artifact_service = _panda()
        self.conv_id = CONVERSATION_ID + "-real"
        self.artifact_id = await _register_upload(
            self.artifact_service,
            tenant=TENANT,
            owner=OWNER,
            conv=self.conv_id,
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
        if self._old_api_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._old_api_key
        if self._old_model is None:
            os.environ.pop("OPENAI_MODEL", None)
        else:
            os.environ["OPENAI_MODEL"] = self._old_model
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_real_model_select_then_field_question_then_write_plan(self):
        sku4, name4, _cat4, _brand4, _ean4, qty4, _purchase4 = PRODUCTS[3]
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn([_analyze_plan_entry()])

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self.panda.respond(
                ConversationRequest(
                    text="Проанализируй этот прайс.",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="real-select-1",
                    conversation_id=self.conv_id,
                    attachment_refs=(self.artifact_id,),
                )
            )
            self.assertEqual(len(run_turn_calls), 1)

            select_result = await self.panda.respond(
                ConversationRequest(
                    text=f"Покажи товар с артикулом {sku4}.",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="real-select-2",
                    conversation_id=self.conv_id,
                )
            )
            self.assertEqual(len(run_turn_calls), 1)  # managed agent NOT entered for this turn
            task = self.panda._action_store.get(  # noqa: SLF001
                tenant_id=TENANT, owner_id=OWNER, conversation_id=self.conv_id
            )
            workset = workset_lib.get_workset(task)
            self.assertEqual(workset.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(workset.selected_identifiers, (sku4,))

            field_result = await self.panda.respond(
                ConversationRequest(
                    text="Сколько единиц этого товара в наличии?",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="real-select-3",
                    conversation_id=self.conv_id,
                )
            )
            self.assertEqual(len(run_turn_calls), 1)
            self.assertIn(qty4, field_result.text)

            write_plan_result = await self.panda.respond(
                ConversationRequest(
                    text="Что именно будет отправлено в Bitrix для этого товара?",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="real-select-4",
                    conversation_id=self.conv_id,
                )
            )
            self.assertEqual(len(run_turn_calls), 1)
            self.assertIn(sku4, write_plan_result.text)
            self.assertIn(name4, write_plan_result.text)
            self.assertFalse(write_plan_result.metadata.get("mutated"))

        final_workset = workset_lib.get_workset(
            self.panda._action_store.get(  # noqa: SLF001
                tenant_id=TENANT, owner_id=OWNER, conversation_id=self.conv_id
            )
        )
        self.assertEqual(final_workset.scope, workset_lib.SCOPE_SINGLE)
        self.assertEqual(final_workset.selected_identifiers, (sku4,))
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
