"""PANDA — CANONICAL TABLE EXECUTION, ONE-SHOT model-call compiler
(``data_intel.nl_plan_llm``) — targeted unit tests.

Covers the model-call -> strict JSON -> validated ``OperationPlan``
boundary in isolation, using a fake ``model_call`` (no network/live
provider): a simple table-wide operation, a COMPOUND request with two
different scopes in ONE model call (the compound-capability gap
``compile_request`` cannot close, see
``tests/test_panda_canonical_table_execution.py``'s
``CompoundScopedOperationContractGapAuditTests``), a second differently-
worded/differently-valued compound example requiring zero code change,
and the deterministic-validation guard rails (non-applicable judgment,
unknown column, malformed JSON, out-of-bounds row range).
"""

from __future__ import annotations

import json
import unittest

from data_intel.contracts import (
    ROLE_BRAND,
    ROLE_PRODUCT_NAME,
    ROLE_SELLING_PRICE,
    ColumnDescriptor,
    TableDescriptor,
)
from data_intel.nl_ops import OP_PERCENT_ROUND, SCOPE_ALL, SCOPE_REMAINDER, SCOPE_ROW_RANGE
from data_intel.nl_plan_llm import ModelPlanNotApplicable, compile_request_via_model


def _table(row_count: int = 4) -> TableDescriptor:
    columns = (
        ColumnDescriptor(source_name="brand", normalized_name="brand", semantic_role=ROLE_BRAND),
        ColumnDescriptor(
            source_name="product_name", normalized_name="product_name", semantic_role=ROLE_PRODUCT_NAME
        ),
        ColumnDescriptor(
            source_name="retail_price", normalized_name="retail_price", semantic_role=ROLE_SELLING_PRICE
        ),
    )
    return TableDescriptor(
        table_id="t1", sheet="Sheet1", range="A1", header_row=1, columns=columns, row_count=row_count
    )


def _fixed_model_call(payload: dict):
    async def _call(prompt: str) -> str:
        assert "retail_price" in prompt  # the table schema really reached the prompt
        return json.dumps(payload)

    return _call


class SimpleTableWideOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_all_scope_operation_compiles(self):
        payload = {
            "applicable": True,
            "wants_workbook": False,
            "operations": [
                {"scope": {"kind": "all"}, "column": "retail_price", "operation": "percent_round", "value": "10"}
            ],
        }
        plan = await compile_request_via_model(
            "Увеличь розничную цену на 10% для всех товаров",
            _table(),
            4,
            model_call=_fixed_model_call(payload),
        )
        self.assertEqual(len(plan.operations), 1)
        op = plan.operations[0]
        self.assertEqual(op.op, OP_PERCENT_ROUND)
        self.assertEqual(op.params["percent"], "10")
        self.assertEqual(op.scope.kind, SCOPE_ALL)

    async def test_operations_missing_defaults_to_all_scope(self):
        payload = {
            "applicable": True,
            "operations": [{"column": "retail_price", "operation": "percent_round", "value": "-8"}],
        }
        plan = await compile_request_via_model(
            "Снизь цену на 8%", _table(), 4, model_call=_fixed_model_call(payload)
        )
        self.assertEqual(plan.operations[0].scope.kind, SCOPE_ALL)
        self.assertEqual(plan.operations[0].params["percent"], "-8")


class CompoundScopedOperationTests(unittest.IsolatedAsyncioTestCase):
    """THE compound-capability closure: one model call, one plan, TWO
    disjoint scopes -- exactly the shape ``compile_request`` cannot
    represent (see the contract-gap audit test)."""

    async def test_first_three_rows_plus_remainder_two_scopes_one_call(self):
        payload = {
            "applicable": True,
            "operations": [
                {
                    "scope": {"kind": "row_range", "start": 0, "end": 3},
                    "column": "retail_price",
                    "operation": "percent_round",
                    "value": "7",
                },
                {
                    "scope": {"kind": "remainder"},
                    "column": "retail_price",
                    "operation": "percent_round",
                    "value": "15",
                },
            ],
        }
        plan = await compile_request_via_model(
            "первым трём +7%, остальным +15%",
            _table(4),
            4,
            model_call=_fixed_model_call(payload),
        )
        self.assertEqual(len(plan.operations), 2)
        first, second = plan.operations
        self.assertEqual(first.scope.kind, SCOPE_ROW_RANGE)
        self.assertEqual((first.scope.start, first.scope.end), (0, 3))
        self.assertEqual(first.params["percent"], "7")
        self.assertEqual(second.scope.kind, SCOPE_REMAINDER)
        self.assertEqual(second.params["percent"], "15")

    async def test_differently_worded_compound_with_different_values_zero_code_change(self):
        """Same compiler code, a semantically DIFFERENT paraphrase and
        DIFFERENT numeric values/row split -- proves the mechanism is
        generic, not a memorized phrase."""
        payload = {
            "applicable": True,
            "operations": [
                {
                    "scope": {"kind": "row_range", "start": 0, "end": 2},
                    "column": "retail_price",
                    "operation": "percent_round",
                    "value": "20",
                },
                {
                    "scope": {"kind": "remainder"},
                    "column": "retail_price",
                    "operation": "percent_round",
                    "value": "-5",
                },
            ],
        }
        plan = await compile_request_via_model(
            "Первым двум позициям поднимите цену на 20%, а на оставшиеся сделайте скидку 5%.",
            _table(4),
            4,
            model_call=_fixed_model_call(payload),
        )
        first, second = plan.operations
        self.assertEqual((first.scope.start, first.scope.end), (0, 2))
        self.assertEqual(first.params["percent"], "20")
        self.assertEqual(second.scope.kind, SCOPE_REMAINDER)
        self.assertEqual(second.params["percent"], "-5")


class ValidationGuardRailTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_marks_not_applicable(self):
        payload = {"applicable": False, "operations": []}
        with self.assertRaises(ModelPlanNotApplicable):
            await compile_request_via_model(
                "Покажи товар с артикулом ABC", _table(), 4, model_call=_fixed_model_call(payload)
            )

    async def test_unknown_column_rejected(self):
        payload = {
            "applicable": True,
            "operations": [
                {"scope": {"kind": "all"}, "column": "not_a_real_column", "operation": "percent_round", "value": "5"}
            ],
        }
        with self.assertRaises(ModelPlanNotApplicable):
            await compile_request_via_model("текст", _table(), 4, model_call=_fixed_model_call(payload))

    async def test_disallowed_operation_rejected(self):
        payload = {
            "applicable": True,
            "operations": [{"scope": {"kind": "all"}, "column": "retail_price", "operation": "delete_row", "value": 1}],
        }
        with self.assertRaises(ModelPlanNotApplicable):
            await compile_request_via_model("текст", _table(), 4, model_call=_fixed_model_call(payload))

    async def test_row_range_out_of_bounds_rejected(self):
        payload = {
            "applicable": True,
            "operations": [
                {
                    "scope": {"kind": "row_range", "start": 0, "end": 999},
                    "column": "retail_price",
                    "operation": "percent_round",
                    "value": "5",
                }
            ],
        }
        with self.assertRaises(ModelPlanNotApplicable):
            await compile_request_via_model("текст", _table(), 4, model_call=_fixed_model_call(payload))

    async def test_malformed_json_rejected(self):
        async def _call(prompt: str) -> str:
            return "this is not json at all"

        with self.assertRaises(ModelPlanNotApplicable):
            await compile_request_via_model("текст", _table(), 4, model_call=_call)

    async def test_markdown_fenced_json_is_still_parsed(self):
        payload = {
            "applicable": True,
            "operations": [
                {"scope": {"kind": "all"}, "column": "retail_price", "operation": "percent_round", "value": "3"}
            ],
        }

        async def _call(prompt: str) -> str:
            return "```json\n" + json.dumps(payload) + "\n```"

        plan = await compile_request_via_model("текст", _table(), 4, model_call=_call)
        self.assertEqual(plan.operations[0].params["percent"], "3")

    async def test_provider_failure_becomes_not_applicable(self):
        async def _call(prompt: str) -> str:
            raise RuntimeError("network down")

        with self.assertRaises(ModelPlanNotApplicable):
            await compile_request_via_model("текст", _table(), 4, model_call=_call)


if __name__ == "__main__":
    unittest.main()
