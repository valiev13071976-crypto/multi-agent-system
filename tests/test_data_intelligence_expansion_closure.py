"""Excel / Data Intelligence — applied expansion closure tests."""

from __future__ import annotations

import unittest

from data_intel.contracts import (
    ROLE_ARTICLE,
    ROLE_INN,
    ROLE_PRICE,
    ROLE_SKU,
    SCHEMA_VERSION,
)
from data_intel.errors import FORMULA_VALIDATION_FAILED, DataIntelError
from data_intel.formulas import sanitize_cell_text, validate_formula
from data_intel.identifiers_ru import normalize_inn
from data_intel.large import LargeDatasetPolicy, build_row_batch_plan
from data_intel.mapping import map_columns
from data_intel.planner import LARGE_BATCH_ROWS, plan_data_job
from task_queue.lanes import LANE_BULK


class ContractTests(unittest.TestCase):
    def test_schema_version_present(self):
        self.assertTrue(SCHEMA_VERSION)

    def test_semantic_roles_defined(self):
        for role in (ROLE_SKU, ROLE_INN, ROLE_PRICE):
            self.assertTrue(role)


class InnValidationTests(unittest.TestCase):
    def test_valid_inn10(self):
        # Known valid test INN with correct checksum
        result = normalize_inn("7707083893")
        self.assertTrue(result.valid)
        self.assertEqual(result.normalized, "7707083893")

    def test_invalid_checksum(self):
        result = normalize_inn("7707083890")
        self.assertFalse(result.valid)

    def test_leading_zeros_preserved(self):
        result = normalize_inn("001234567890")
        self.assertEqual(result.normalized, "001234567890")


class FormulaInjectionTests(unittest.TestCase):
    def test_sanitize_prefixes(self):
        self.assertTrue(sanitize_cell_text("=HYPERLINK()").startswith("'"))

    def test_validate_rejects_cmd(self):
        with self.assertRaises(DataIntelError) as ctx:
            validate_formula("=cmd|'/C calc'!A0")
        self.assertEqual(ctx.exception.reason, FORMULA_VALIDATION_FAILED)


class LargeDataPolicyTests(unittest.TestCase):
    def test_large_rows_require_async(self):
        policy = LargeDatasetPolicy()
        self.assertTrue(policy.requires_async(row_count=LARGE_BATCH_ROWS))

    def test_row_batches_bounded(self):
        plan = build_row_batch_plan(
            dataset_id="ds-1",
            tenant_id="tenant-a",
            row_count=2500,
            rows_per_batch=500,
        )
        self.assertEqual(plan["batch_count"], 5)
        self.assertTrue(all(b["bounded"] for b in plan["batches"]))


class BatchPlannerTests(unittest.TestCase):
    def test_heavy_ops_stamp_bulk(self):
        planned = plan_data_job(
            dataset_id="ds-big",
            tenant_id="tenant-a",
            operations=("reconcile",),
            row_count=LARGE_BATCH_ROWS,
        )
        self.assertEqual(planned.execution_lane, LANE_BULK)
        self.assertTrue(planned.enqueue)


class ColumnMappingTests(unittest.TestCase):
    def test_deterministic_ru_aliases_without_llm(self):
        headers = ["Артикул", "ИНН", "Цена"]
        rows = [{"Артикул": "A1", "ИНН": "7707083893", "Цена": "100"}]
        cols = map_columns(headers, rows)
        roles = {c.source_name: c.semantic_role for c in cols}
        self.assertEqual(roles["Артикул"], ROLE_ARTICLE)
        self.assertEqual(roles["ИНН"], ROLE_INN)
        self.assertEqual(roles["Цена"], ROLE_PRICE)


class LlmBoundaryTests(unittest.TestCase):
    def test_mapping_works_without_llm_suggestions(self):
        """Architectural invariant: column mapping is deterministic-first."""
        cols = map_columns(["SKU", "price"], [{"SKU": "x", "price": "1"}])
        self.assertEqual(len(cols), 2)
        self.assertEqual(cols[0].semantic_role, ROLE_SKU)


if __name__ == "__main__":
    unittest.main()
