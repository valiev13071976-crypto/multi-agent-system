"""Unit tests for CSV formula-like cells kept as strings."""

from __future__ import annotations

import unittest

from documents.models import SOURCE_TEST_FIXTURE, DocumentIngestRequest
from documents.parsers.csv_parser import CsvDocumentParser
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="proj-csv"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class DocumentCsvTests(unittest.TestCase):
    def test_formula_like_cells_as_strings(self):
        raw = b"name,amount\nAda,=1+2\nBob,+SUM(1)\n"
        parsed = CsvDocumentParser().parse(
            document_id="d-csv",
            data=raw,
            filename="sheet.csv",
            limits={"max_text_bytes": 100_000, "max_table_cells": 10_000},
        )
        self.assertEqual(parsed.metadata_safe.get("formula_like_cells"), 2)
        self.assertTrue(parsed.tables)
        table = parsed.tables[0]
        flat = [c for row in table.rows for c in row]
        self.assertIn("=1+2", flat)
        self.assertIn("+SUM(1)", flat)
        # Values remain strings — never numeric results
        self.assertNotIn("3", flat)

        svc = DocumentService(InMemoryDocumentStore())
        row = svc.ingest(
            DocumentIngestRequest(
                scope=_scope(),
                filename="sheet.csv",
                content=raw,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="csv-1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertEqual(row.document_type, "csv")
        self.assertGreater(row.chunk_count, 0)


if __name__ == "__main__":
    unittest.main()
