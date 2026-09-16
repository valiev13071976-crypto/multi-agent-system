"""PANDA -- CANONICAL TABLE EXECUTION, ONE-SHOT model-call compiler
(data_intel.nl_plan_llm) -- targeted unit tests.

Covers the model-call -> strict JSON -> validated OperationPlan boundary
in isolation, using a fake model_call (no network/live provider): a
simple table-wide operation, a COMPOUND request with two different
scopes in ONE model call, a second differently-worded/differently-valued
compound example requiring zero code change, the deterministic-
validation guard rails, and the PR #93 production defect closure
(column names the model cannot reproduce verbatim, resolved via stable
column ids instead; and the STATUS taxonomy distinguishing a genuine
NOT_APPLICABLE judgment from a technical MODEL_ERROR/PARSE_ERROR/
VALIDATION_ERROR failure).
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
from data_intel.nl_plan_llm import (
    STATUS_MODEL_ERROR,
    STATUS_NOT_APPLICABLE,
    STATUS_PARSE_ERROR,
    STATUS_VALIDATION_ERROR,
    ModelPlanError,
    compile_request_via_model,
)

# The exact production column name (PR #93): contains an internal comma,
# which a model can fail to reproduce verbatim if asked to echo it back
# as free text -- this is why the prompt now exposes only stable ids.
PRODUCTION_COLUMN = "Предоплата, Цена с НДС"


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


def _production_table(row_count: int = 4) -> TableDescriptor:
    columns = (
        ColumnDescriptor(source_name="sku", normalized_name="sku", semantic_role="sku"),
        ColumnDescriptor(
            source_name="product_name", normalized_name="product_name", semantic_role=ROLE_PRODUCT_NAME
        ),
        ColumnDescriptor(
            source_name=PRODUCTION_COLUMN, normalized_name="prepay_price_vat", semantic_role=ROLE_SELLING_PRICE
        ),
    )
    return TableDescriptor(
        table_id="t1", sheet="Sheet1", range="A1", header_row=1, columns=columns, row_count=row_count
    )


def _fixed_model_call(payload: dict):
    async def _call(prompt: str) -> str:
        assert "c0" in prompt and "c1" in prompt  # stable column ids reached the prompt
        return json.dumps(payload)

    return _call


class SimpleTableWideOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_all_scope_operation_compiles(self):
        payload = {
            "kind": "table_operation",
            "wants_workbook": False,
            "operations": [
                {"scope": {"kind": "all"}, "column_id": "c2", "operation": "percent_round", "value": "10"}
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
        self.assertEqual(op.params["column"], "retail_price")
        self.assertEqual(op.params["percent"], "10")
        self.assertEqual(op.scope.kind, SCOPE_ALL)

    async def test_operations_missing_scope_defaults_to_all(self):
        payload = {
            "kind": "table_operation",
            "operations": [{"column_id": "c2", "operation": "percent_round", "value": "-8"}],
        }
        plan = await compile_request_via_model(
            "Снизь цену на 8%", _table(), 4, model_call=_fixed_model_call(payload)
        )
        self.assertEqual(plan.operations[0].scope.kind, SCOPE_ALL)
        self.assertEqual(plan.operations[0].params["percent"], "-8")


class CompoundScopedOperationTests(unittest.IsolatedAsyncioTestCase):
    """THE compound-capability closure: one model call, one plan, TWO
    disjoint scopes -- exactly the shape compile_request cannot
    represent (see the contract-gap audit test in
    tests/test_panda_canonical_table_execution.py)."""

    async def test_first_three_rows_plus_remainder_two_scopes_one_call(self):
        payload = {
            "kind": "table_operation",
            "operations": [
                {
                    "scope": {"kind": "row_range", "start": 0, "end": 3},
                    "column_id": "c2",
                    "operation": "percent_round",
                    "value": "7",
                },
                {
                    "scope": {"kind": "remainder"},
                    "column_id": "c2",
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
            "kind": "table_operation",
            "operations": [
                {
                    "scope": {"kind": "row_range", "start": 0, "end": 2},
                    "column_id": "c2",
                    "operation": "percent_round",
                    "value": "20",
                },
                {
                    "scope": {"kind": "remainder"},
                    "column_id": "c2",
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


class BitrixQualifierDoesNotBecomeNotApplicableTests(unittest.IsolatedAsyncioTestCase):
    """PR #93 requirement 2: a request to transform a local table IS a
    table operation even when it also explicitly says not to write the
    result to Bitrix/an external system -- that qualifier must not, by
    itself, cause the compiler to reject a plan whose model output
    legitimately marks the request applicable. (The prompt now carries
    this rule explicitly for the model; this test only proves that the
    deterministic parser/validator layer -- the part this repo controls
    outright -- does not itself treat a "wants_workbook"/no-export
    signal as a rejection reason.)"""

    async def test_table_operation_kind_with_bitrix_qualifier_text_still_compiles(self):
        payload = {
            "kind": "table_operation",
            "wants_workbook": False,
            "operations": [
                {"scope": {"kind": "all"}, "column_id": "c2", "operation": "percent_round", "value": "7"}
            ],
        }
        plan = await compile_request_via_model(
            "Увеличь цену на 7% для всех товаров. Ничего в Bitrix не записывай.",
            _table(),
            4,
            model_call=_fixed_model_call(payload),
        )
        self.assertEqual(len(plan.operations), 1)
        self.assertFalse(plan.wants_workbook)


class ColumnIdResolutionTests(unittest.IsolatedAsyncioTestCase):
    """PR #93 requirement 3 / production defect closure: the model never
    has to reproduce an arbitrary column string (e.g. one containing a
    comma) -- it returns a stable column_id, resolved deterministically
    back to the real column name. An unknown id fails validation; there
    is no fuzzy-matching fallback."""

    async def test_column_with_comma_resolved_via_stable_id_not_by_name(self):
        async def _call(prompt: str) -> str:
            self.assertIn('c2 = "Предоплата, Цена с НДС"', prompt)
            payload = {
                "kind": "table_operation",
                "operations": [
                    {
                        "scope": {"kind": "row_range", "start": 0, "end": 3},
                        "column_id": "c2",
                        "operation": "percent_round",
                        "value": "7",
                    },
                    {
                        "scope": {"kind": "remainder"},
                        "column_id": "c2",
                        "operation": "percent_round",
                        "value": "15",
                    },
                ],
            }
            return json.dumps(payload)

        plan = await compile_request_via_model(
            "Для первых трёх строк увеличь цену в столбце «Предоплата, Цена с НДС» на 7%, "
            "для остальных строк — на 15%.",
            _production_table(4),
            4,
            model_call=_call,
        )
        self.assertEqual(len(plan.operations), 2)
        for op in plan.operations:
            self.assertEqual(op.params["column"], PRODUCTION_COLUMN)

    async def test_model_echoing_truncated_column_name_instead_of_id_fails_validation(self):
        """Reproduces the ACTUAL PR #93 production defect: if the model
        (incorrectly) echoes back a mangled/truncated column string
        instead of the id it was given -- exactly what a real
        gpt-4o-mini call did for this column before this fix -- that must
        fail deterministic validation as an unknown column id (never a
        silent, wrong-column mutation)."""

        async def _call(prompt: str) -> str:
            payload = {
                "kind": "table_operation",
                "operations": [
                    {
                        "scope": {"kind": "all"},
                        "column_id": "Предоплата",  # NOT a valid c<N> id
                        "operation": "percent_round",
                        "value": "7",
                    }
                ],
            }
            return json.dumps(payload)

        with self.assertRaises(ModelPlanError) as ctx:
            await compile_request_via_model("текст", _production_table(4), 4, model_call=_call)
        self.assertEqual(ctx.exception.status, STATUS_VALIDATION_ERROR)
        self.assertEqual(ctx.exception.reason_code, "unknown_column_id")

    async def test_unknown_column_id_rejected(self):
        payload = {
            "kind": "table_operation",
            "operations": [
                {"scope": {"kind": "all"}, "column_id": "c99", "operation": "percent_round", "value": "5"}
            ],
        }
        with self.assertRaises(ModelPlanError) as ctx:
            await compile_request_via_model("текст", _table(), 4, model_call=_fixed_model_call(payload))
        self.assertEqual(ctx.exception.status, STATUS_VALIDATION_ERROR)
        self.assertEqual(ctx.exception.reason_code, "unknown_column_id")


class StatusTaxonomyTests(unittest.IsolatedAsyncioTestCase):
    """PR #93 requirement 5: a caller must be able to distinguish a
    genuine NOT_APPLICABLE model judgment from every other TECHNICAL
    failure (MODEL_ERROR/PARSE_ERROR/VALIDATION_ERROR) -- exactly the
    distinction the production defect's silent-fallthrough bug erased."""

    async def test_model_marks_not_applicable(self):
        payload = {"kind": "not_applicable"}
        with self.assertRaises(ModelPlanError) as ctx:
            await compile_request_via_model(
                "Покажи товар с артикулом ABC", _table(), 4, model_call=_fixed_model_call(payload)
            )
        self.assertEqual(ctx.exception.status, STATUS_NOT_APPLICABLE)

    async def test_disallowed_operation_is_validation_error_not_not_applicable(self):
        payload = {
            "kind": "table_operation",
            "operations": [{"scope": {"kind": "all"}, "column_id": "c2", "operation": "delete_row", "value": 1}],
        }
        with self.assertRaises(ModelPlanError) as ctx:
            await compile_request_via_model("текст", _table(), 4, model_call=_fixed_model_call(payload))
        self.assertEqual(ctx.exception.status, STATUS_VALIDATION_ERROR)

    async def test_row_range_out_of_bounds_is_validation_error(self):
        payload = {
            "kind": "table_operation",
            "operations": [
                {
                    "scope": {"kind": "row_range", "start": 0, "end": 999},
                    "column_id": "c2",
                    "operation": "percent_round",
                    "value": "5",
                }
            ],
        }
        with self.assertRaises(ModelPlanError) as ctx:
            await compile_request_via_model("текст", _table(), 4, model_call=_fixed_model_call(payload))
        self.assertEqual(ctx.exception.status, STATUS_VALIDATION_ERROR)
        self.assertEqual(ctx.exception.reason_code, "row_range_out_of_bounds")

    async def test_malformed_json_is_parse_error_not_not_applicable(self):
        async def _call(prompt: str) -> str:
            return "this is not json at all"

        with self.assertRaises(ModelPlanError) as ctx:
            await compile_request_via_model("текст", _table(), 4, model_call=_call)
        self.assertEqual(ctx.exception.status, STATUS_PARSE_ERROR)

    async def test_missing_kind_is_parse_error(self):
        async def _call(prompt: str) -> str:
            return json.dumps({"operations": []})

        with self.assertRaises(ModelPlanError) as ctx:
            await compile_request_via_model("текст", _table(), 4, model_call=_call)
        self.assertEqual(ctx.exception.status, STATUS_PARSE_ERROR)

    async def test_markdown_fenced_json_is_still_parsed(self):
        payload = {
            "kind": "table_operation",
            "operations": [
                {"scope": {"kind": "all"}, "column_id": "c2", "operation": "percent_round", "value": "3"}
            ],
        }

        async def _call(prompt: str) -> str:
            return "```json\n" + json.dumps(payload) + "\n```"

        plan = await compile_request_via_model("текст", _table(), 4, model_call=_call)
        self.assertEqual(plan.operations[0].params["percent"], "3")

    async def test_provider_failure_is_model_error_not_not_applicable(self):
        async def _call(prompt: str) -> str:
            raise RuntimeError("network down")

        with self.assertRaises(ModelPlanError) as ctx:
            await compile_request_via_model("текст", _table(), 4, model_call=_call)
        self.assertEqual(ctx.exception.status, STATUS_MODEL_ERROR)
        self.assertEqual(ctx.exception.reason_code, "model_call_failed")


if __name__ == "__main__":
    unittest.main()
