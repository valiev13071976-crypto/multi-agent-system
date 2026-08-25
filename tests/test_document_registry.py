"""Unit tests for document parser registry."""

from __future__ import annotations

import unittest

from documents.errors import DocumentError
from documents.models import DOC_CSV, DOC_DOCX, DOC_MD, DOC_PDF, DOC_TXT, DOC_XLSX
from documents.parsers import DocumentParserRegistry, build_default_registry, parser_registry_snapshot


class DocumentRegistryTests(unittest.TestCase):
    def test_default_registry_supports_core_types(self):
        reg = build_default_registry(max_file_bytes=1_000_000)
        supported = set(reg.list_supported_types())
        for t in (DOC_TXT, DOC_MD, DOC_CSV, DOC_XLSX, DOC_DOCX, DOC_PDF):
            self.assertIn(t, supported)
            self.assertIsNotNone(reg.get_parser(t))

    def test_registry_frozen_after_build(self):
        reg = build_default_registry(max_file_bytes=1000)

        class Dummy:
            parser_id = "dummy"
            version = "0"
            supported_types = ("txt",)

            def parse(self, **kwargs):
                raise NotImplementedError

        with self.assertRaises(RuntimeError):
            reg.register(Dummy(), max_size=1000)

    def test_unknown_type_raises(self):
        reg = DocumentParserRegistry()
        with self.assertRaises(DocumentError) as ctx:
            reg.get_parser("html")
        self.assertEqual(ctx.exception.reason, "unsupported_document_type")

    def test_snapshot_lists_parsers(self):
        snap = parser_registry_snapshot()
        self.assertIn("xlsx", snap["supported_types"])
        self.assertTrue(snap["parsers"])


if __name__ == "__main__":
    unittest.main()
