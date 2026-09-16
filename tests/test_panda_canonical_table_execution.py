"""PANDA — CANONICAL TABLE EXECUTION (NL -> structured operation -> existing
deterministic executor).

PRODUCTION DEFECT (after PR #91 fixed canonical Workset ownership): a
real ``PANDA_MANAGED_AGENT_ENABLED=true`` production request --

    "Увеличь розничную цену ... для всех товаров и покажи результат.
    Ничего в Bitrix не записывай."

-- did NOT execute the table transformation. Panda saw the data and even
computed aggregate stats, but answered conversationally ("Я не могу тут
массово пересчитать и вывести все строки...") instead of ever reaching
the EXISTING deterministic Data Intelligence executor.

ROOT CAUSE: the managed-agent integration boundary
(``managed_agent_poc.runtime_subprocess``) exposes exactly 3 READ-ONLY
tools -- ``analyze_spreadsheet`` / ``select_product`` /
``explain_bitrix_write_plan`` -- none of which can perform a bulk/
structural table transform. ``WorkflowPandaConversationGateway.respond()``
handed every eligible turn to that boundary FIRST; when its own tools
could not satisfy a table-wide numeric request, the model's own free-text
answer was returned as-is, and the turn never reached the EXISTING,
already-proven ``data_intel.nl_ops.compile_request`` ->
``data_intel.transform.execute_plan`` chain (reached today only through
the ``data.excel_assistant``/``assist`` tool call) that a managed-agent-
DISABLED conversation already uses successfully (see
``tests/test_panda_canonical_workset_single_data_ownership.py``'s own
``WorksetAcceptanceIntegrationTests``).

FIX: ``WorkflowPandaConversationGateway._maybe_execute_canonical_table_
operation`` (see ``business_assistant/conversation_gateway.py``) is tried
BEFORE the managed-agent boundary for every eligible turn. It gives the
EXISTING ``data.excel_assistant`` tool call the text FIRST; a result is
used (and the managed-agent boundary is skipped entirely for this turn)
ONLY when that tool's own ``status == "OK"`` -- i.e. ``compile_request``
ACTUALLY compiled and ``execute_plan`` ACTUALLY executed a genuine
table-wide operation. Every other outcome (no canonical dataset yet,
single-row lookup, plain analysis, ambiguous) returns ``None`` with zero
side effects, so the managed agent keeps handling every other
conversational/product turn exactly as before -- it never "competes"
with a genuine table operation, and it is never touched at all for one.

No new agent/router/dataset store/executor/NL vocabulary is introduced:
this reuses the SAME NL->IR compiler, the SAME deterministic executor,
and the SAME canonical Workset (PR #91) every other FAMILY_EXCEL turn
already uses.

REGRESSION CLOSURE (found by this same task's own broader regression
run): the FIRST version of the fix gated purely on the tool's own
``status == "OK"``, which is not sufficient on its own -- ``compile_
request``'s intentionally loose ``_KEEP_ONLY_RE`` grammar spuriously
matched a "...покажи ... и отдельно укажи ТОЛЬКО те поля, для
которых..." clause inside a real, long, explicit product-enrichment
request as a ``filter_contains`` op, which (once executed) also reports
``status == "OK"`` -- destructively filtering the dataset down to 0 rows
and hijacking a turn that should have gone to the managed agent's
product-enrichment delegation. The fix adds a precedence guard that
reuses ``resolve_action_turn``'s OWN four pure, pre-existing, text-only
classifiers (``is_explicit_bitrix_write_confirmation``, ``is_explicit_
product_enrichment_request``, ``is_bitrix_write_plan_question``,
``is_explicit_product_pricing_or_category_refinement_request``) as an
up-front skip gate, in the SAME precedence order ``resolve_action_turn``
itself already applies -- not a new phrase/stem list. See
``test_production_enrichment_text_with_coincidental_keep_only_wording_
still_uses_managed_agent`` below for the regression test, and the full
managed-agent regression suites (``test_panda_managed_agent_enrichment_
delegation.py``, ``test_panda_managed_agent_governed_write_confirmation_
defect_closure.py``) for the broader confirmation that no other turn
shape is affected.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from business_assistant import workset as workset_lib
from business_assistant.action_continuation import EXCEL_CONTRACT
from business_assistant.conversation_gateway import ConversationRequest
from managed_agent_poc.adapter import ManagedAgentPOC
from managed_agent_poc.panda_bridge import ENABLED_ENV_VAR
from tests.test_panda_canonical_workset_single_data_ownership import (
    FILENAME,
    NAME_A,
    OWNER,
    PURCHASE_A,
    RETAIL_A,
    SKU_A,
    SKU_B,
    TENANT,
    _first_dataset_store,
    _xlsx_bytes,
)
from tests.test_panda_managed_agent_enrichment_delegation import (
    PRODUCTION_TEXT,
    _make_fake_run_turn,
    _raw_tool_fields,
)
from tests.test_panda_product_enrichment_conversational import _panda, _register_upload

CONVERSATION_ID = "conv-table-exec"


def _analyze_plan_entry() -> dict:
    return {
        "tool_calls": [
            {
                "tool": "analyze_spreadsheet",
                "output": {"row_count": 4, "column_count": 7},
            }
        ],
        "final_output": "В таблице 4 строки.",
    }


def _select_product_plan_entry(*, identifier: str, name: str, purchase_price: str, retail_price: str) -> dict:
    return {
        "current_identifier": identifier,
        "tool_calls": [
            {
                "tool": "select_product",
                "output": {
                    "status": "SELECTED",
                    "matched_by": "next_unspecified",
                    "name": name,
                    "sku": identifier,
                    "category": "Телевизоры",
                    "brand": "LG",
                    "purchase_price": purchase_price,
                    "retail_price": retail_price,
                },
            }
        ],
        "final_output": f"Выбран товар {name}.",
    }


def _tracking_fake_run_turn(plan: list[dict]):
    """Wraps ``_make_fake_run_turn`` (the existing, proven test double for
    ``ManagedAgentPOC.run_turn``) so the test can assert exactly how many
    times -- and with what text -- the managed-agent boundary was
    actually entered."""
    fake = _make_fake_run_turn(plan)
    calls: list[dict] = []

    def wrapper(self, **kwargs):
        calls.append(kwargs)
        return fake(self, **kwargs)

    return wrapper, calls


class CanonicalTableExecutionAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """MANDATORY ACCEPTANCE: one uploaded multi-row spreadsheet; turn 1
    analyzes it; turn 2 selects/inspects one product; turn 3 (NO
    reattachment) requests a table-wide numeric transformation in natural
    language -- with the managed-agent boundary ENABLED end to end
    through the real ``WorkflowPandaConversationGateway.respond()`` entry
    point, exactly the production configuration the reported defect came
    from."""

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

    def _workset(self):
        task = self.panda._action_store.get(  # noqa: SLF001
            tenant_id=TENANT, owner_id=OWNER, conversation_id=CONVERSATION_ID
        )
        return workset_lib.get_workset(task)

    async def _run_three_turn_journey(self, *, turn3_text: str, request_prefix: str):
        plan = [
            _analyze_plan_entry(),
            _select_product_plan_entry(
                identifier=SKU_A, name=NAME_A, purchase_price=PURCHASE_A, retail_price=RETAIL_A
            ),
        ]
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn(plan)

        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            r1 = await self.panda.respond(
                ConversationRequest(
                    text="Проанализируй этот прайс.",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id=f"{request_prefix}-1",
                    conversation_id=CONVERSATION_ID,
                    attachment_refs=(self.artifact_id,),
                )
            )
            r2 = await self.panda.respond(
                ConversationRequest(
                    text="Возьми первый товар из этого прайса.",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id=f"{request_prefix}-2",
                    conversation_id=CONVERSATION_ID,
                )
            )
            # Sanity: turns 1/2 above DID go through the managed-agent
            # boundary (this reproduces the real production shape) --
            # narrowing scope to SINGLE product A, exactly the state a
            # later bulk request must not stay pinned to.
            self.assertEqual(len(run_turn_calls), 2)
            w_single = self._workset()
            self.assertEqual(w_single.scope, workset_lib.SCOPE_SINGLE)
            self.assertEqual(w_single.selected_identifiers, (SKU_A,))

            # Turn 3: NO reattachment, table-wide numeric transformation.
            r3 = await self.panda.respond(
                ConversationRequest(
                    text=turn3_text,
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id=f"{request_prefix}-3",
                    conversation_id=CONVERSATION_ID,
                )
            )
            # THE CORE ASSERTION: the managed-agent boundary was NEVER
            # entered for the table-wide turn -- it must not "compete"
            # with the canonical table execution path (task directive).
            self.assertEqual(len(run_turn_calls), 2)

        return r1, r2, r3

    async def test_mandatory_acceptance_table_wide_increase_after_single_product_selection(self):
        source_id = None

        r1, r2, r3 = await self._run_three_turn_journey(
            turn3_text="Увеличь розничную цену на 10% для всех товаров и покажи результат. "
            "Ничего в Bitrix не записывай.",
            request_prefix="rep",
        )

        # No "attach the file"/"cannot mass recalculate" conversational
        # refusal of any kind.
        self.assertNotIn("Приложите файл", r3.text)
        self.assertNotIn("не могу", r3.text.casefold())

        w_final = self._workset()
        self.assertIsNotNone(w_final)
        # Same canonical Workset throughout -- established on turn 1 and
        # never replaced.
        w_after_attach = w_final  # placeholder resolved below
        self.assertTrue(w_final.source_dataset_id)
        source_id = w_final.source_dataset_id

        # A genuine table-wide transform was executed: scope resets to
        # FULL_DATASET (never stays pinned to product A), row count is
        # unchanged (a pure percent bump touches every row), and a NEW
        # derived dataset was produced (deterministic version chain).
        self.assertEqual(w_final.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(w_final.selected_identifiers, ())
        self.assertNotEqual(w_final.current_dataset_id, source_id)

        # Concrete, real result -- not a vague placeholder/refusal.
        self.assertIn("Строк было: 4, стало: 4", r3.text)
        preview = r3.metadata.get("table_operation_preview") or {}
        self.assertEqual(preview.get("row_count_before"), 4)
        self.assertEqual(preview.get("row_count_after"), 4)
        changed_rows = preview.get("changed_rows") or []
        self.assertEqual(len(changed_rows), 4)
        first = changed_rows[0]
        self.assertEqual(first["operation"], "percent_round")
        self.assertEqual(first["percent"], "10")
        self.assertTrue(first["source_value"])
        self.assertTrue(first["resulting_value"])
        self.assertNotEqual(first["source_value"], first["resulting_value"])
        # Identifier survives in the row projection (SKU column present).
        self.assertTrue(any(str(v) == SKU_A for v in first["row"].values()))

        self.assertTrue(r3.metadata.get("canonical_table_execution"))
        self.assertEqual(r3.metadata.get("action_decision"), "CALL_TOOL")

        # Zero Bitrix mutation -- no bridge was even wired for this test.
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001
        # No MaxTurnsExceeded / managed-agent error surfaced anywhere.
        self.assertNotIn("MaxTurnsExceeded", r3.text)

        # Original dataset itself is untouched/still fully readable.
        store = _first_dataset_store(self.panda)
        original_rows = store.get_rows(source_id, tenant_id=TENANT)
        self.assertEqual(len(original_rows), 4)

    async def test_second_semantically_different_wording_and_value_same_mechanism(self):
        """A second, semantically DIFFERENT wording with a DIFFERENT
        numeric value and the opposite sign (a decrease, via ``снизь``
        rather than an explicit ``+``) -- proves this is the general
        ``compile_request`` grammar, not one memorized phrase; changing
        wording/numbers requires zero code change."""
        r1, r2, r3 = await self._run_three_turn_journey(
            turn3_text="Снизь розничную цену на 8% по всем позициям, покажи, что получилось.",
            request_prefix="rep2",
        )

        self.assertNotIn("Приложите файл", r3.text)
        w_final = self._workset()
        self.assertEqual(w_final.scope, workset_lib.SCOPE_FULL_DATASET)
        self.assertEqual(w_final.selected_identifiers, ())

        preview = r3.metadata.get("table_operation_preview") or {}
        changed_rows = preview.get("changed_rows") or []
        self.assertEqual(len(changed_rows), 4)
        first = changed_rows[0]
        self.assertEqual(first["percent"], "-8")
        self.assertTrue(any(str(v) == SKU_A for v in first["row"].values()))
        self.assertIsNone(self.panda._bitrix_bridge)  # noqa: SLF001

    async def test_single_product_and_plain_analysis_turns_still_use_managed_agent_unaffected(self):
        """Regression: turns that are NOT a genuine table-wide operation
        (a bare "analyze" turn, and a single-product selection) are
        completely unaffected -- they still go through the managed-agent
        boundary exactly as before this fix. Only a turn ``compile_
        request`` itself can compile is ever intercepted."""
        plan = [
            _analyze_plan_entry(),
            _select_product_plan_entry(
                identifier=SKU_B, name="Телевизор B", purchase_price="95000", retail_price="139990"
            ),
        ]
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn(plan)
        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            await self.panda.respond(
                ConversationRequest(
                    text="Проанализируй этот прайс.",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="reg-1",
                    conversation_id=CONVERSATION_ID,
                    attachment_refs=(self.artifact_id,),
                )
            )
            self.assertEqual(len(run_turn_calls), 1)
            await self.panda.respond(
                ConversationRequest(
                    text="Покажи второй товар из прайса.",
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="reg-2",
                    conversation_id=CONVERSATION_ID,
                )
            )
            self.assertEqual(len(run_turn_calls), 2)

    async def test_production_enrichment_text_with_coincidental_keep_only_wording_still_uses_managed_agent(self):
        """Regression closure: ``compile_request``'s own loose ``_KEEP_ONLY_RE``
        grammar spuriously matches "...укажи ТОЛЬКО те поля, для которых..."
        inside the real production full-card-preparation request as a
        ``filter_contains`` op that, once executed, DOES report
        ``status == "OK"`` -- even though the user never asked for a
        table-wide operation. This turn is an explicit product-enrichment
        request (``is_explicit_product_enrichment_request``, the SAME
        pure, pre-existing classifier ``resolve_action_turn`` itself
        checks with top precedence), so it must be skipped by the
        canonical-table-execution probe and reach the managed agent
        exactly as before this fix -- never silently turned into a
        4-row-losing table filter."""
        plan = [
            {
                "current_identifier": SKU_A,
                "tool_calls": [
                    {
                        "tool": "select_product",
                        "output": {
                            "status": "SELECTED",
                            "matched_by": "next_unspecified",
                            **_raw_tool_fields(
                                name=NAME_A, sku=SKU_A, ean="8806096796849",
                                purchase_price=PURCHASE_A, retail_price=RETAIL_A,
                            ),
                        },
                    }
                ],
                "final_output": f"Товар {NAME_A} подготовлен.",
            }
        ]
        fake_run_turn, run_turn_calls = _tracking_fake_run_turn(plan)
        with mock.patch.object(ManagedAgentPOC, "run_turn", new=fake_run_turn):
            result = await self.panda.respond(
                ConversationRequest(
                    text=PRODUCTION_TEXT,
                    tenant_id=TENANT,
                    user_id=OWNER,
                    request_id="prod-enrich-1",
                    conversation_id=CONVERSATION_ID,
                    attachment_refs=(self.artifact_id,),
                )
            )
        self.assertEqual(len(run_turn_calls), 1)
        self.assertEqual(result.metadata.get("action_decision"), "MANAGED_AGENT")
        self.assertIsNone(result.metadata.get("canonical_table_execution"))
        # The dataset must NOT have been destructively filtered down to 0
        # rows by the spurious "keep only" match.
        store = _first_dataset_store(self.panda)
        w = self._workset()
        rows = store.get_rows(w.current_dataset_id, tenant_id=TENANT)
        self.assertEqual(len(rows), 4)


class CompoundScopedOperationContractGapAuditTests(unittest.TestCase):
    """COMPOUND CAPABILITY AUDIT (task directive): proves -- rather than
    silently assumes -- that the EXISTING structured contract
    (``data_intel.nl_ops.OperationPlan``/``PlannedOperation``, compiled by
    ``compile_request``) cannot express "scope A -> transformation A,
    remainder/scope B -> transformation B" in a single request. This is
    reported/documented as a gap; it is NOT fixed here -- doing so with
    phrase-specific regex for "первые"/"остальные"/hardcoded percentages
    is explicitly forbidden by this task."""

    def test_compile_request_only_ever_compiles_one_percent_rule_per_request(self):
        from data_intel.nl_ops import OP_PERCENT_ROUND, compile_request
        from data_intel.service import DataIntelligenceService
        from data_intel.store import InMemoryDatasetStore

        svc = DataIntelligenceService(InMemoryDatasetStore())
        ingested = svc.ingest(_xlsx_bytes(), filename=FILENAME, tenant_id=TENANT)
        desc = svc.store.get_dataset(ingested["dataset_id"], tenant_id=TENANT)
        table = desc.tables[0]

        plan = compile_request(
            "Первым трём товарам увеличь розничную цену на 7%, "
            "остальным увеличь розничную цену на 15%, покажи результат.",
            table,
        )
        percent_ops = [op for op in plan.operations if op.op == OP_PERCENT_ROUND]
        # THE GAP: exactly ONE percent rule is ever compiled --
        # ``nl_ops._PERCENT_RE.search`` only ever matches the FIRST
        # percentage in the text, and no ``PlannedOperation`` shape
        # carries an ordinal/remainder row-scope split at all. The
        # "first three rows / everyone else" partition described in the
        # text is entirely lost; applying this plan would (incorrectly,
        # for the compound request) apply ONE percentage to the WHOLE
        # table rather than two different percentages to two disjoint
        # row scopes.
        self.assertEqual(len(percent_ops), 1)
        self.assertEqual(percent_ops[0].params["percent"], "7")


if __name__ == "__main__":
    unittest.main()
