"""Unit tests for spreadsheet CellValue / CellRange / WorkbookRecord models."""

from __future__ import annotations

import unittest

from documents.models import (
    CELL_BLANK,
    CELL_FORMULA,
    CELL_NUMBER,
    CELL_STRING,
    CellRange,
    CellValue,
    WorkbookRecord,
    WorksheetRecord,
)


class SpreadsheetModelsTests(unittest.TestCase):
    def test_cell_value_types(self):
        blank = CellValue(row=1, column=1, coordinate="A1", value=None, value_type=CELL_BLANK)
        self.assertIsNone(blank.value)
        num = CellValue(row=1, column=2, coordinate="B1", value="42", value_type=CELL_NUMBER)
        self.assertEqual(num.value, "42")
        text = CellValue(row=1, column=3, coordinate="C1", value="=SUM(1)", value_type=CELL_STRING)
        self.assertTrue(text.value.startswith("="))
        formula = CellValue(
            row=2,
            column=1,
            coordinate="A2",
            value=None,
            value_type=CELL_FORMULA,
            formula="=A1+B1",
        )
        self.assertEqual(formula.formula, "=A1+B1")

    def test_cell_range_bounds(self):
        rng = CellRange(
            sheet_name="Sheet1",
            start_row=1,
            end_row=3,
            start_column=1,
            end_column=2,
            a1_range="A1:B3",
            cell_count=6,
        )
        self.assertEqual(rng.a1_range, "A1:B3")
        with self.assertRaises(ValueError):
            CellRange(
                sheet_name="S",
                start_row=3,
                end_row=1,
                start_column=1,
                end_column=1,
                a1_range="A3:A1",
                cell_count=1,
            )

    def test_workbook_record(self):
        wb = WorkbookRecord(
            document_id="doc-1",
            sheet_names=("Sheet1", "Data"),
            sheet_count=2,
            active_sheet="Sheet1",
            has_macros=False,
            has_external_links=False,
        )
        self.assertEqual(wb.sheet_count, 2)
        self.assertEqual(wb.active_sheet, "Sheet1")
        sheet = WorksheetRecord(sheet_name="Sheet1", index=0, max_row=10, max_column=5)
        self.assertEqual(sheet.sheet_name, "Sheet1")


if __name__ == "__main__":
    unittest.main()
