"""Unit tests for documents.models invariants."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from documents.models import (
    CELL_FORMULA,
    CELL_NUMBER,
    CELL_STRING,
    CELL_TYPES,
    DOCUMENT_TYPES,
    DOC_TXT,
    SOURCE_OPERATOR,
    SOURCE_TEST_FIXTURE,
    STATUS_PARSED,
    CellRange,
    CellValue,
    DocumentIngestRequest,
    DocumentProvenance,
    DocumentRecord,
    WorkbookRecord,
    citation_ref_for,
    content_hash_bytes,
    sanitize_filename,
)
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _prov(**kwargs):
    defaults = dict(
        source_type=SOURCE_OPERATOR,
        source_id="src-1",
        ingested_by="test",
        ingested_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return DocumentProvenance(**defaults)


class DocumentModelsTests(unittest.TestCase):
    def test_document_types_include_txt(self):
        self.assertIn(DOC_TXT, DOCUMENT_TYPES)

    def test_sanitize_filename_strips_path(self):
        self.assertEqual(sanitize_filename("../../etc/passwd.txt"), "passwd.txt")
        self.assertEqual(sanitize_filename(""), "unnamed")

    def test_content_hash_stable(self):
        self.assertEqual(content_hash_bytes(b"abc"), content_hash_bytes(b"abc"))
        self.assertNotEqual(content_hash_bytes(b"abc"), content_hash_bytes(b"abd"))

    def test_ingest_request_requires_bytes(self):
        with self.assertRaises(ValueError):
            DocumentIngestRequest(
                scope=_scope(),
                filename="a.txt",
                content="not-bytes",  # type: ignore[arg-type]
                source_type=SOURCE_OPERATOR,
                source_id="s1",
            )

    def test_ingest_request_rejects_bad_source(self):
        with self.assertRaises(ValueError):
            DocumentIngestRequest(
                scope=_scope(),
                filename="a.txt",
                content=b"x",
                source_type="nope",
                source_id="s1",
            )

    def test_provenance_and_record(self):
        stamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        prov = _prov()
        row = DocumentRecord(
            document_id="d1",
            scope=_scope(),
            filename_safe="note.txt",
            media_type="text/plain",
            document_type=DOC_TXT,
            size_bytes=1,
            content_hash="h",
            source_type=SOURCE_OPERATOR,
            source_ref="s1",
            provenance=prov,
            sensitivity=SENSITIVITY_INTERNAL,
            status=STATUS_PARSED,
            created_at=stamp,
            updated_at=stamp,
        )
        self.assertEqual(row.document_type, DOC_TXT)
        self.assertEqual(citation_ref_for("d1", "c1"), "document:d1#chunk:c1")

    def test_cell_and_workbook_models(self):
        cell = CellValue(row=1, column=1, coordinate="A1", value="1", value_type=CELL_NUMBER)
        self.assertIn(cell.value_type, CELL_TYPES)
        formula = CellValue(
            row=2,
            column=1,
            coordinate="A2",
            value=None,
            value_type=CELL_FORMULA,
            formula="=A1+1",
        )
        self.assertEqual(formula.formula, "=A1+1")
        rng = CellRange(
            sheet_name="Sheet",
            start_row=1,
            end_row=2,
            start_column=1,
            end_column=1,
            a1_range="A1:A2",
            cell_count=2,
        )
        self.assertEqual(rng.cell_count, 2)
        wb = WorkbookRecord(document_id="d1", sheet_names=("Sheet",), sheet_count=1)
        self.assertEqual(wb.sheet_count, 1)
        with self.assertRaises(ValueError):
            CellValue(row=1, column=1, coordinate="A1", value="x", value_type="bogus")

    def test_source_test_fixture_allowed(self):
        req = DocumentIngestRequest(
            scope=_scope(),
            filename="f.txt",
            content=b"hi",
            source_type=SOURCE_TEST_FIXTURE,
            source_id="fx-1",
            sensitivity=SENSITIVITY_INTERNAL,
        )
        self.assertEqual(req.source_type, SOURCE_TEST_FIXTURE)


if __name__ == "__main__":
    unittest.main()
