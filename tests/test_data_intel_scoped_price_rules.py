"""Data Intelligence -- reusable deterministic scoped-price-rule primitives
(business-task-ownership/workset-continuation defect closure, PR #90
correction; SECOND correction, post architecture review).

============================================================
WHY THIS FILE EXISTS (replaces
tests/test_panda_compound_scoped_price_rules_defect_closure.py)
============================================================

PR #90's first correction wired a COMPOUND scoped-rule capability
(multiple DIFFERENT percent changes over multiple DIFFERENT, non-
overlapping row scopes in ONE call, e.g. "first three rows +7%, the rest
+15%") into a managed conversational agent's own private-dataset tool,
then bridged the tool's derived result back into the shared ``data_intel``
store via a generated workbook -> artifact -> re-ingest round trip. A
second architecture review correctly identified that round trip as
reinforcing exactly the private/shared dual-dataset-ownership split this
whole defect-closure effort is meant to eliminate, and required it to be
removed pending a canonical Workset/structured-operation-plan phase.

This file proves that the underlying DETERMINISTIC PRIMITIVES that
correction introduced are genuinely reusable and worth keeping
independently of that removed wiring:

- ``data_intel.nl_ops.validate_scoped_price_rules`` -- structural
  validation of an untrusted, already-produced rule structure against the
  REAL table schema and a small, explicit, non-extensible whitelist of
  scope/operation shapes. Never evaluates an expression, never accepts an
  arbitrary column name.
- ``data_intel.transform.execute_scoped_percent_rules`` -- deterministic
  per-scope arithmetic (``Decimal``-exact) with a per-row before/after
  ``row_changes`` result, so ANY future caller can build a concrete
  preview without re-deriving numbers from free text.
- ``data_intel.service.DataIntelligenceService.execute_scoped_price_rules``
  -- orchestrates validate + execute + persists the result as a fresh
  derived dataset in the SAME canonical store/service instance (never a
  second dataset universe).

NONE of these three reference ``managed_agent_poc`` or any conversational
gateway at all -- they are pure ``data_intel``-layer building blocks,
intentionally UNWIRED to any production caller right now, kept as an
Operation-IR primitive for whichever semantic seam the future canonical
Workset phase settles on.

Every scenario below uses ARBITRARY percentages/row segmentations/scope
kinds (never the specific numbers from the original production
reproduction), and a second scenario per level proves that changing those
values/kinds requires ZERO production-code change -- only different
input data.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from data_intel.contracts import (
    ROLE_ARTICLE,
    ROLE_BRAND,
    ROLE_CATEGORY,
    ROLE_EAN,
    ROLE_PRODUCT_NAME,
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    ColumnDescriptor,
)
from data_intel.nl_ops import UnsupportedOperationError, validate_scoped_price_rules
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore
from data_intel.transform import execute_scoped_percent_rules
from tests.test_panda_business_task_workset_continuation_defect_closure import (
    LG_SKU_1,
    LG_SKU_2,
    LG_SKU_3,
    SAMSUNG_SKU_1,
    SAMSUNG_SKU_2,
    _five_row_two_brand_price_list,
)

COLUMNS = ("sku", "product_name", "category", "brand", "ean", "purchase_price", "розница")


class _FakeTable:
    """Minimal stand-in for ``data_intel.contracts.TableDescriptor`` --
    ``validate_scoped_price_rules`` only ever reads ``.columns``."""

    def __init__(self, columns):
        self.columns = columns


def _column_descriptors() -> tuple[ColumnDescriptor, ...]:
    roles = {
        "sku": ROLE_ARTICLE,
        "product_name": ROLE_PRODUCT_NAME,
        "category": ROLE_CATEGORY,
        "brand": ROLE_BRAND,
        "ean": ROLE_EAN,
        "purchase_price": ROLE_PURCHASE_PRICE,
        "розница": ROLE_SELLING_PRICE,
    }
    return tuple(
        ColumnDescriptor(source_name=col, normalized_name=col.lower(), inferred_type="text", semantic_role=roles[col])
        for col in COLUMNS
    )


def _rows_dicts() -> list[dict]:
    import io

    from openpyxl import load_workbook

    raw = _five_row_two_brand_price_list()
    wb = load_workbook(io.BytesIO(raw))
    ws = wb.active
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=False):
        rows.append({header[i]: str(cell.value) for i, cell in enumerate(row)})
    return rows


class ValidateScopedPriceRulesTests(unittest.TestCase):
    """Structural validation: a small, explicit, non-extensible whitelist
    of scope/operation shapes, never a wording dictionary."""

    def setUp(self):
        self.table = _FakeTable(_column_descriptors())

    def test_row_position_range_and_remainder_validate(self):
        rules = [
            {"scope": {"kind": "row_position_range", "start_position": 1, "end_position": 2}, "price_field": "retail_price", "percent": 9},
            {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": -6},
        ]
        resolved = validate_scoped_price_rules(rules, self.table)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(resolved[0]["scope"]["kind"], "row_position_range")
        self.assertEqual(resolved[0]["column"], "розница")
        self.assertEqual(resolved[0]["percent"], "9")
        self.assertEqual(resolved[1]["scope"]["kind"], "remainder")
        self.assertEqual(resolved[1]["percent"], "-6")

    def test_text_contains_and_remainder_validate_with_different_values(self):
        """Different scope kind, different field, different percentages --
        same function, zero code change."""
        rules = [
            {"scope": {"kind": "text_contains", "text_field": "brand", "contains": "Samsung"}, "price_field": "retail_price", "percent": 17},
            {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": -2},
        ]
        resolved = validate_scoped_price_rules(rules, self.table)
        self.assertEqual(resolved[0]["scope"]["column"], "brand")
        self.assertEqual(resolved[0]["scope"]["contains"], "Samsung")
        self.assertEqual(resolved[0]["percent"], "17")

    def test_price_compare_scope_validates(self):
        rules = [
            {
                "scope": {"kind": "price_compare", "price_field": "purchase_price", "operator": "gte", "threshold": "100000"},
                "price_field": "retail_price",
                "percent": 4,
            },
            {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": 1},
        ]
        resolved = validate_scoped_price_rules(rules, self.table)
        self.assertEqual(resolved[0]["scope"]["operator"], "gte")
        self.assertEqual(resolved[0]["scope"]["threshold"], "100000")

    def test_rejects_unknown_scope_kind(self):
        with self.assertRaises(UnsupportedOperationError):
            validate_scoped_price_rules(
                [{"scope": {"kind": "everything_ever"}, "price_field": "retail_price", "percent": 5}], self.table
            )

    def test_rejects_unknown_price_field(self):
        with self.assertRaises(UnsupportedOperationError):
            validate_scoped_price_rules(
                [{"scope": {"kind": "remainder"}, "price_field": "does_not_exist", "percent": 5}], self.table
            )

    def test_rejects_price_compare_missing_threshold(self):
        with self.assertRaises(UnsupportedOperationError):
            validate_scoped_price_rules(
                [
                    {
                        "scope": {"kind": "price_compare", "price_field": "purchase_price", "operator": "gte"},
                        "price_field": "retail_price",
                        "percent": 5,
                    }
                ],
                self.table,
            )

    def test_rejects_empty_rule_list(self):
        with self.assertRaises(UnsupportedOperationError):
            validate_scoped_price_rules([], self.table)

    def test_rejects_missing_percent(self):
        with self.assertRaises(UnsupportedOperationError):
            validate_scoped_price_rules([{"scope": {"kind": "remainder"}, "price_field": "retail_price"}], self.table)


class ExecuteScopedPercentRulesTests(unittest.TestCase):
    """Deterministic execution: per-row before/after tracking over
    non-overlapping scopes, arbitrary values."""

    def test_position_range_and_remainder_apply_disjoint_changes(self):
        columns = _column_descriptors()
        rows = _rows_dicts()
        rules = validate_scoped_price_rules(
            [
                {"scope": {"kind": "row_position_range", "start_position": 1, "end_position": 2}, "price_field": "retail_price", "percent": 8},
                {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": -3},
            ],
            _FakeTable(columns),
        )
        result = execute_scoped_percent_rules(rows, columns, rules)
        self.assertEqual(result.row_count_before, 5)
        self.assertEqual(result.row_count_after, 5)
        self.assertEqual(len(result.row_changes), 5, "every row belongs to exactly one of the two scopes")

        by_sku = {r["sku"]: r for r in result.rows}
        self.assertEqual(Decimal(by_sku[LG_SKU_1]["розница"]), Decimal("162000.00"))
        self.assertEqual(Decimal(by_sku[LG_SKU_2]["розница"]), Decimal("194400.00"))
        self.assertEqual(Decimal(by_sku[SAMSUNG_SKU_1]["розница"]), Decimal("135800.00"))
        self.assertEqual(Decimal(by_sku[SAMSUNG_SKU_2]["розница"]), Decimal("140650.00"))
        self.assertEqual(Decimal(by_sku[LG_SKU_3]["розница"]), Decimal("116400.00"))

        changes_by_row = {c["row_index"]: c for c in result.row_changes}
        self.assertEqual(changes_by_row[0]["percent"], "8")
        self.assertEqual(changes_by_row[0]["rule_index"], 0)
        self.assertEqual(changes_by_row[2]["percent"], "-3")
        self.assertEqual(changes_by_row[2]["rule_index"], 1)

    def test_brand_filter_and_remainder_with_different_values(self):
        """A DIFFERENT scope kind (text filter, not a position range) and
        DIFFERENT percentages -- same function, zero code change needed to
        support a new segmentation shape or new numbers."""
        columns = _column_descriptors()
        rows = _rows_dicts()
        rules = validate_scoped_price_rules(
            [
                {"scope": {"kind": "text_contains", "text_field": "brand", "contains": "LG"}, "price_field": "retail_price", "percent": 12},
                {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": -4},
            ],
            _FakeTable(columns),
        )
        result = execute_scoped_percent_rules(rows, columns, rules)
        by_sku = {r["sku"]: r for r in result.rows}
        self.assertEqual(Decimal(by_sku[LG_SKU_1]["розница"]), Decimal("168000.00"))
        self.assertEqual(Decimal(by_sku[LG_SKU_2]["розница"]), Decimal("201600.00"))
        self.assertEqual(Decimal(by_sku[LG_SKU_3]["розница"]), Decimal("134400.00"))
        self.assertEqual(Decimal(by_sku[SAMSUNG_SKU_1]["розница"]), Decimal("134400.00"))
        self.assertEqual(Decimal(by_sku[SAMSUNG_SKU_2]["розница"]), Decimal("139200.00"))

    def test_rows_outside_every_scope_remain_completely_unchanged(self):
        """A rule set that only claims a strict SUBSET of rows must leave
        the rest byte-for-byte untouched -- full coverage is never
        required."""
        columns = _column_descriptors()
        rows = _rows_dicts()
        rules = validate_scoped_price_rules(
            [{"scope": {"kind": "text_contains", "text_field": "brand", "contains": "LG"}, "price_field": "retail_price", "percent": 10}],
            _FakeTable(columns),
        )
        result = execute_scoped_percent_rules(rows, columns, rules)
        by_sku = {r["sku"]: r for r in result.rows}
        self.assertEqual(len(result.row_changes), 3, "only the 3 LG rows are claimed")
        self.assertEqual(Decimal(by_sku[SAMSUNG_SKU_1]["розница"]), Decimal("140000"))
        self.assertEqual(Decimal(by_sku[SAMSUNG_SKU_2]["розница"]), Decimal("145000"))

    def test_later_rule_never_reclaims_a_row_an_earlier_rule_already_matched(self):
        """A 'remainder' scope after an earlier, narrower scope must never
        double-apply a change to the same row, even if the scopes would
        otherwise overlap."""
        columns = _column_descriptors()
        rows = _rows_dicts()
        rules = validate_scoped_price_rules(
            [
                {"scope": {"kind": "text_contains", "text_field": "brand", "contains": "LG"}, "price_field": "retail_price", "percent": 10},
                {"scope": {"kind": "text_contains", "text_field": "brand", "contains": "L"}, "price_field": "retail_price", "percent": 999},
            ],
            _FakeTable(columns),
        )
        result = execute_scoped_percent_rules(rows, columns, rules)
        by_sku = {r["sku"]: r for r in result.rows}
        # All three LG rows match the FIRST, narrower rule ("LG") and are
        # claimed there; the second, broader rule ("L", 999%) must match
        # NOTHING already claimed -- so no LG price shows the absurd 999%.
        self.assertEqual(Decimal(by_sku[LG_SKU_1]["розница"]), Decimal("165000.00"))
        self.assertEqual(Decimal(by_sku[LG_SKU_2]["розница"]), Decimal("198000.00"))
        self.assertEqual(Decimal(by_sku[LG_SKU_3]["розница"]), Decimal("132000.00"))


class DataIntelligenceServiceScopedPriceRulesTests(unittest.TestCase):
    """Full round trip through the canonical service/store: validate +
    execute + persist as a fresh derived dataset in the SAME store --
    never a second dataset universe."""

    def setUp(self):
        self.svc = DataIntelligenceService(InMemoryDatasetStore())
        ing = self.svc.ingest(_five_row_two_brand_price_list(), filename="prices.xlsx", tenant_id="tenant-a", enqueue_large=False)
        self.dataset_id = ing["dataset_id"]

    def _rows_by_sku(self, dataset_id: str) -> dict:
        desc = self.svc.store.get_dataset(dataset_id, tenant_id="tenant-a")
        table = desc.tables[0]
        rows = self.svc.store.get_rows(dataset_id, tenant_id="tenant-a", table_id=table.table_id)
        return {r["sku"]: r for r in rows}

    def test_compound_rules_produce_a_new_derived_dataset_leaving_the_original_untouched(self):
        rules = [
            {"scope": {"kind": "row_position_range", "start_position": 1, "end_position": 2}, "price_field": "retail_price", "percent": 8},
            {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": -3},
        ]
        result = self.svc.execute_scoped_price_rules(self.dataset_id, rules, tenant_id="tenant-a")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["rows_changed"], 5)
        new_dataset_id = result["dataset_id"]
        self.assertNotEqual(new_dataset_id, self.dataset_id)

        preview_by_id = {p["identifier"]: p for p in result["preview_rows"]}
        self.assertEqual(preview_by_id[LG_SKU_1]["before"], "150000")
        self.assertEqual(preview_by_id[LG_SKU_1]["after"], "162000.00")
        self.assertEqual(preview_by_id[LG_SKU_1]["percent"], "8")

        new_rows = self._rows_by_sku(new_dataset_id)
        self.assertEqual(Decimal(new_rows[LG_SKU_1]["розница"]), Decimal("162000.00"))
        self.assertEqual(Decimal(new_rows[SAMSUNG_SKU_1]["розница"]), Decimal("135800.00"))

        original_rows = self._rows_by_sku(self.dataset_id)
        self.assertEqual(Decimal(original_rows[LG_SKU_1]["розница"]), Decimal("150000"))
        self.assertEqual(Decimal(original_rows[SAMSUNG_SKU_1]["розница"]), Decimal("140000"))

    def test_second_wording_class_same_mechanism_zero_code_change(self):
        """Different scope kind, different percentages, different
        direction (discount on the LARGER group) -- proves the service
        method generalizes without any per-scenario code."""
        rules = [
            {"scope": {"kind": "text_contains", "text_field": "brand", "contains": "LG"}, "price_field": "retail_price", "percent": 12},
            {"scope": {"kind": "remainder"}, "price_field": "retail_price", "percent": -4},
        ]
        result = self.svc.execute_scoped_price_rules(self.dataset_id, rules, tenant_id="tenant-a")
        self.assertEqual(result["status"], "OK")
        new_rows = self._rows_by_sku(result["dataset_id"])
        self.assertEqual(Decimal(new_rows[LG_SKU_2]["розница"]), Decimal("201600.00"))
        self.assertEqual(Decimal(new_rows[SAMSUNG_SKU_2]["розница"]), Decimal("139200.00"))

    def test_invalid_rules_return_typed_unsupported_status_never_a_crash(self):
        result = self.svc.execute_scoped_price_rules(
            self.dataset_id,
            [{"scope": {"kind": "not_a_real_kind"}, "price_field": "retail_price", "percent": 5}],
            tenant_id="tenant-a",
        )
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertEqual(result["dataset_id"], self.dataset_id, "an unsupported request never mutates or replaces the dataset")


if __name__ == "__main__":
    unittest.main()
