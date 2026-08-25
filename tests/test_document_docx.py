"""Unit tests for DOCX paragraph extraction."""

from __future__ import annotations

import io
import unittest

from docx import Document

from documents.models import SOURCE_TEST_FIXTURE, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="proj-docx"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _docx_bytes(*paragraphs: str) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class DocumentDocxTests(unittest.TestCase):
    def test_paragraph_extract(self):
        data = _docx_bytes("First paragraph about documents.", "Second paragraph continues.")
        svc = DocumentService(InMemoryDocumentStore())
        scope = _scope()
        row = svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename="memo.docx",
                content=data,
                source_type=SOURCE_TEST_FIXTURE,
                source_id="docx-1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertEqual(row.document_type, "docx")
        parsed = svc._parsed_cache[row.document_id]
        texts = [b.text for b in parsed.text_blocks]
        self.assertTrue(any("First paragraph" in t for t in texts))
        self.assertTrue(any("Second paragraph" in t for t in texts))
        chunks = svc.list_chunks(row.document_id, requesting_scope=scope)
        self.assertTrue(chunks)


if __name__ == "__main__":
    unittest.main()
