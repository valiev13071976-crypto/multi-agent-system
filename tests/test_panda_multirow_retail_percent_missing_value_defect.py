"""Regression test for a real production defect found during the
canonical single-authority acceptance journey (post PR #96).

PRODUCTION SCENARIO
--------------------
1. Upload a 5-row price list (only ``purchase_price`` exists).
2. Select ONE product and ask to raise ITS retail price by X% above
   purchase -- this creates a derived retail-price column
   (``price_role="retail"``) but ONLY for that single selected row
   (``OP_ADD_COLUMN_PERCENT`` scoped to ``row_range`` of size 1).
3. Without re-uploading, ask (in natural language) for a multi-row
   retail-price increase covering ALL rows, split across two scopes
   (first N rows +X%, remainder +Y%).

DEFECT
------
``data_intel.transform._apply_percent_round`` only mutates a row when the
TARGET column already holds a numeric value for that row
(``v = _dec(row.get(column)); if v is not None: ...``). For the four rows
that never went through step 2, the derived retail column is still
missing/blank, so ``percent_round`` silently no-ops on them: the executor
reports "row changed" (it is inside the requested scope) but the resulting
value is unchanged/blank. Only the ONE previously-selected row (which
already had a retail value) is genuinely recalculated. The user is told
"5 rows were, 5 rows are now" while 4 of 5 rows received NO real price
change at all -- this is exactly the "команда применяется к требуемым
строкам" requirement the acceptance journey demands, and it silently
fails for any row that has not yet been individually touched.

FIX
---
When the scoped operation's target column is a derived retail/selling
price column (``semantic_role == ROLE_SELLING_PRICE``) and a given row has
no value there yet, ``_apply_percent_round`` now derives a starting value
from that SAME row's purchase-price column (``semantic_role ==
ROLE_PURCHASE_PRICE``) before applying the percent -- exactly the same
base ``_apply_add_column_percent`` already uses the first time it creates
this column. ``purchase_price`` itself is never read back into or
overwritten by this fallback; it is only ever the READ-ONLY basis for a
missing retail value, preserving the ``purchase_price != retail_price``
invariant end-to-end.
"""

from __future__ import annotations

import io
import unittest
from decimal import ROUND_HALF_UP, Decimal


TENANT = "tenant-multirow-retail-fix"

SKU_A, PURCHASE_A = "TV-P0-0001", "10000.00"
SKU_B, PURCHASE_B = "TV-P1-0002", "12500.00"
SKU_C, PURCHASE_C = "TV-P2-0003", "9000.00"
SKU_D, PURCHASE_D = "TV-P3-0004", "17750.00"
SKU_E, PURCHASE_E = "TV-P4-0005", "13300.00"


def _five_row_xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "quantity", "purchase_price"])
    ws.append([SKU_A, "Product Alpha", "TV", "LG", "4600000000101", "3", PURCHASE_A])
    ws.append([SKU_B, "Product Bravo", "TV", "LG", "4600000000102", "8", PURCHASE_B])
    ws.append([SKU_C, "Product Charlie", "TV", "LG", "4600000000103", "15", PURCHASE_C])
    ws.append([SKU_D, "Product Delta", "TV", "LG", "4600000000104", "21", PURCHASE_D])
    ws.append([SKU_E, "Product Echo", "TV", "LG", "4600000000105", "6", PURCHASE_E])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class MultiRowRetailPercentMissingValueDefectClosureTests(unittest.TestCase):
    """Reproduces the exact two-step production shape deterministically
    (no model call needed -- this exercises the deterministic executor
    seam directly, exactly like the existing
    ``CompoundScopedOperationContractGapAuditTests`` in
    ``tests/test_panda_canonical_table_execution.py``)."""

    def setUp(self):
        from data_intel.service import DataIntelligenceService
        from data_intel.store import InMemoryDatasetStore

        self.svc = DataIntelligenceService(InMemoryDatasetStore())
        ingested = self.svc.ingest(_five_row_xlsx_bytes(), filename="five.xlsx", tenant_id=TENANT)
        self.dataset_id = ingested["dataset_id"]

    def _table_and_rows(self, dataset_id):
        desc = self.svc.store.get_dataset(dataset_id, tenant_id=TENANT)
        return desc.tables[0], self.svc.store.get_rows(dataset_id, tenant_id=TENANT)

    def test_multirow_percent_round_derives_missing_retail_value_from_purchase_price(self):
        from data_intel.nl_ops import (
            OP_ADD_COLUMN_PERCENT,
            OP_PERCENT_ROUND,
            OperationPlan,
            OperationScope,
            PlannedOperation,
            SCOPE_REMAINDER,
            SCOPE_ROW_RANGE,
        )
        from data_intel.transform import execute_plan

        table, rows = self._table_and_rows(self.dataset_id)
        self.assertEqual(len(rows), 5)

        # Step 2 (production turn 3): selected product is row index 3
        # (Delta) -- raise ITS retail price 7% above purchase. Mirrors the
        # exact real model output observed in production/local repro:
        # OP_ADD_COLUMN_PERCENT scoped to a single-row row_range.
        step2_plan = OperationPlan(
            operations=(
                PlannedOperation(
                    OP_ADD_COLUMN_PERCENT,
                    {
                        "source_column": "purchase_price",
                        "new_column": "retail_price",
                        "percent": "7",
                        "price_role": "retail",
                    },
                    scope=OperationScope(kind=SCOPE_ROW_RANGE, start=3, end=4),
                ),
            ),
        )
        step2 = execute_plan(rows, table.columns, step2_plan)
        self.assertEqual(
            Decimal(step2.rows[3]["retail_price"]),
            (Decimal(PURCHASE_D) * Decimal("1.07")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        # Only Delta has a retail_price; the other four rows do not yet.
        for i in (0, 1, 2, 4):
            self.assertNotIn("retail_price", step2.rows[i])
        # purchase_price untouched by the derived-column creation.
        for i, expected in enumerate((PURCHASE_A, PURCHASE_B, PURCHASE_C, PURCHASE_D, PURCHASE_E)):
            self.assertEqual(step2.rows[i]["purchase_price"], expected)

        # Step 3 (production turn 5, without re-upload): multi-row escape
        # -- first two rows +10%, remainder +5% -- targeting the SAME
        # retail_price column, now split across ALL 5 rows.
        step3_plan = OperationPlan(
            operations=(
                PlannedOperation(
                    OP_PERCENT_ROUND,
                    {"column": "retail_price", "percent": "10", "round_mode": None, "round_to": None},
                    scope=OperationScope(kind=SCOPE_ROW_RANGE, start=0, end=2),
                ),
                PlannedOperation(
                    OP_PERCENT_ROUND,
                    {"column": "retail_price", "percent": "5", "round_mode": None, "round_to": None},
                    scope=OperationScope(kind=SCOPE_REMAINDER),
                ),
            ),
        )
        step3 = execute_plan(step2.rows, step2.columns, step3_plan)

        # DEFECT (pre-fix): rows 0,1,2,4 kept no/blank retail_price because
        # percent_round no-ops when the column is missing for that row.
        # FIX: those rows now derive a fresh retail_price from THEIR OWN
        # purchase_price, using the requested percent for their scope.
        self.assertEqual(
            Decimal(step3.rows[0]["retail_price"]),
            (Decimal(PURCHASE_A) * Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        self.assertEqual(
            Decimal(step3.rows[1]["retail_price"]),
            (Decimal(PURCHASE_B) * Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        self.assertEqual(
            Decimal(step3.rows[2]["retail_price"]),
            (Decimal(PURCHASE_C) * Decimal("1.05")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        self.assertEqual(
            Decimal(step3.rows[4]["retail_price"]),
            (Decimal(PURCHASE_E) * Decimal("1.05")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        # Delta (row 3) already had a retail_price from step 2 -- percent
        # COMPOUNDS on its existing retail value, it is NOT re-derived
        # from purchase_price a second time.
        expected_delta = (
            (Decimal(PURCHASE_D) * Decimal("1.07")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * Decimal("1.05")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.assertEqual(Decimal(step3.rows[3]["retail_price"]), expected_delta)

        # purchase_price is NEVER mutated by this fallback -- it is only
        # ever a read-only basis for a missing retail value.
        for i, expected in enumerate((PURCHASE_A, PURCHASE_B, PURCHASE_C, PURCHASE_D, PURCHASE_E)):
            self.assertEqual(step3.rows[i]["purchase_price"], expected)

    def test_plain_purchase_price_percent_round_is_completely_unaffected(self):
        """No behavior change for the pre-existing, non-derived-price
        shape: an in-place percent change directly on ``purchase_price``
        itself (semantic_role ROLE_PURCHASE_PRICE, not ROLE_SELLING_PRICE)
        never triggers the new fallback -- rows that never had a
        purchase_price stay untouched, exactly like before this fix."""
        from data_intel.nl_ops import (
            OP_PERCENT_ROUND,
            OperationPlan,
            OperationScope,
            PlannedOperation,
            SCOPE_ROW_RANGE,
        )
        from data_intel.transform import execute_plan

        table, rows = self._table_and_rows(self.dataset_id)
        plan = OperationPlan(
            operations=(
                PlannedOperation(
                    OP_PERCENT_ROUND,
                    {"column": "purchase_price", "percent": "10", "round_mode": None, "round_to": None},
                    scope=OperationScope(kind=SCOPE_ROW_RANGE, start=0, end=2),
                ),
            ),
        )
        result = execute_plan(rows, table.columns, plan)
        self.assertEqual(
            Decimal(result.rows[0]["purchase_price"]),
            (Decimal(PURCHASE_A) * Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        # Untouched rows (outside scope) keep their original value.
        self.assertEqual(result.rows[2]["purchase_price"], PURCHASE_C)


if __name__ == "__main__":
    unittest.main()
