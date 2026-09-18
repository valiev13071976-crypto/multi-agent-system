"""PANDA -- DEFECT C closure: downloadable Excel must be a normal,
user-facing result, never an internal dataset/debug export.

PRODUCTION FACT: the workbook downloaded after a table operation exposed
internal dataset/debug structure -- ``SUMMARY``/``ISSUES``/``Provenance``
sheets, plus DUPLICATED columns (the row's own original source header
AND a separate role-alias key ``DataIntelligenceService.ingest`` already
adds onto every row for internal lookups, e.g. both "product_name" (the
source header, in this fixture) and its own role alias would collide
into ONE alias key here -- the general shape of the bug is a source
header whose name differs from its resolved role, e.g. a Russian header
"Наименование" gaining a SEPARATE "product_name" alias key alongside it).

FIX: ``DataIntelligenceService.generate_excel(kind="business_result")``
renders a single, clean sheet using the SCHEMA's own declared column list
(``table.columns`` -- alias-free, original order) instead of the raw
in-memory row dict's keys, and skips the SUMMARY/ISSUES/Provenance sheets
entirely (``data_intel.excel_out.generate_user_result_workbook``). Wired
as the default export for the "download the result" turn
(``data_intel.tools.DataIntelToolAdapter._assist``/``_assist_structured``)
-- the original debug-rich ``kind="data"`` export remains available,
unchanged, for any caller that explicitly asks for it.
"""

from __future__ import annotations

import io
import unittest

TENANT = "tenant-user-facing-export"


def _xlsx_with_russian_headers() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    # A Russian header ("Наименование") whose RESOLVED role
    # ("product_name") is a DIFFERENT string -- exactly the shape that
    # produces a duplicate alias key in the row dict.
    ws.append(["Артикул", "Наименование", "Закупочная цена"])
    ws.append(["SKU-100", "Товар Один", "1000.00"])
    ws.append(["SKU-200", "Товар Два", "2000.00"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class UserFacingWorkbookExportTests(unittest.TestCase):
    def setUp(self):
        from data_intel.service import DataIntelligenceService
        from data_intel.store import InMemoryDatasetStore

        self.svc = DataIntelligenceService(InMemoryDatasetStore())
        ingested = self.svc.ingest(
            _xlsx_with_russian_headers(), filename="Прайс сентябрь (финал).xlsx", tenant_id=TENANT
        )
        self.dataset_id = ingested["dataset_id"]

    def _load(self, content: bytes):
        from openpyxl import load_workbook

        return load_workbook(io.BytesIO(content), read_only=False)

    def test_business_result_export_has_no_debug_sheets(self):
        result = self.svc.generate_excel(self.dataset_id, tenant_id=TENANT, kind="business_result")
        wb = self._load(result["content"])
        self.assertNotIn("SUMMARY", wb.sheetnames)
        self.assertNotIn("ISSUES", wb.sheetnames)
        self.assertNotIn("Provenance", wb.sheetnames)
        self.assertNotIn("RESULT", wb.sheetnames)
        self.assertEqual(len(wb.sheetnames), 1)

    def test_business_result_export_has_no_duplicate_alias_columns(self):
        result = self.svc.generate_excel(self.dataset_id, tenant_id=TENANT, kind="business_result")
        wb = self._load(result["content"])
        ws = wb[wb.sheetnames[0]]
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        # Exactly the ORIGINAL source headers, in ORIGINAL order -- no
        # "product_name"/"sku"/"article"/"purchase_price"/etc. role-alias
        # duplicate column anywhere.
        self.assertEqual(headers, ["Артикул", "Наименование", "Закупочная цена"])
        self.assertEqual(len(headers), len(set(headers)))

    def test_business_result_export_preserves_row_values(self):
        result = self.svc.generate_excel(self.dataset_id, tenant_id=TENANT, kind="business_result")
        wb = self._load(result["content"])
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("SKU-100", "Товар Один", "1000.00"))
        self.assertEqual(rows[1], ("SKU-200", "Товар Два", "2000.00"))

    def test_business_result_export_uses_sensible_filename(self):
        result = self.svc.generate_excel(self.dataset_id, tenant_id=TENANT, kind="business_result")
        self.assertNotEqual(result["filename"], "dataset.xlsx")
        self.assertTrue(result["filename"].endswith(".xlsx"))
        self.assertIn("Прайс сентябрь (финал)", result["filename"])

    def test_legacy_debug_export_kind_is_unchanged(self):
        """Backward compatibility: an explicit ``kind="data"`` request
        (the pre-existing standalone ``generate_excel`` tool operation)
        keeps its original debug-rich shape -- zero behavior change for
        any caller that still wants it."""
        result = self.svc.generate_excel(self.dataset_id, tenant_id=TENANT, kind="data")
        wb = self._load(result["content"])
        self.assertIn("SUMMARY", wb.sheetnames)
        self.assertIn("RESULT", wb.sheetnames)
        self.assertIn("ISSUES", wb.sheetnames)
        self.assertIn("Provenance", wb.sheetnames)
        self.assertEqual(result["filename"], "dataset.xlsx")


if __name__ == "__main__":
    unittest.main()
