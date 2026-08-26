"""Eval coverage for P14 documents/spreadsheets core suite handlers."""

from __future__ import annotations

import unittest

from evals.handlers import get_handler
from evals.models import EvalCase
from evals.versions import CORE_SUITE_VERSION


DOCUMENT_HANDLERS = (
    "document_cross_scope_denied",
    "document_path_traversal_denied",
    "document_too_large_denied",
    "document_xlsx_formula_not_executed",
    "document_macros_denied",
    "document_external_link_not_fetched",
    "document_pdf_requires_ocr",
    "document_sensitive_encrypted",
    "document_chunk_provenance_preserved",
    "document_malformed_archive_rejected",
    "document_no_public_api",
)


class EvalDocumentsSpreadsheetsTests(unittest.TestCase):
    def test_core_suite_version(self):
        self.assertEqual(CORE_SUITE_VERSION, "1.7.0")

    def test_documents_core_handlers_pass(self):
        for name in DOCUMENT_HANDLERS:
            with self.subTest(name=name):
                case = EvalCase(
                    case_id=name,
                    suite_id="core",
                    case_version="1",
                    category="documents_spreadsheets",
                    description=name,
                    handler=name,
                    critical=True,
                )
                result = get_handler(name)(case)
                self.assertTrue(result["passed"], msg=f"{name}:{result.get('reason_codes')}")


if __name__ == "__main__":
    unittest.main()
