"""Unit tests for DocumentService ingest / parse / chunks."""

from __future__ import annotations

import unittest

from documents.models import SOURCE_OPERATOR, STATUS_PARSED, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="proj-svc"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class DocumentServiceTests(unittest.TestCase):
    def test_ingest_txt_parsed_with_chunks(self):
        svc = DocumentService(InMemoryDocumentStore())
        scope = _scope()
        row = svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename="note.txt",
                content=b"Hello documents foundation.",
                source_type=SOURCE_OPERATOR,
                source_id="op-1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertEqual(row.status, STATUS_PARSED)
        self.assertEqual(row.document_type, "txt")
        self.assertGreater(row.chunk_count, 0)
        chunks = svc.list_chunks(row.document_id, requesting_scope=scope)
        self.assertTrue(chunks)
        self.assertIn("Hello documents", chunks[0].content_safe or "")
        got = svc.get(row.document_id, requesting_scope=scope)
        self.assertIsNotNone(got)
        self.assertEqual(got.document_id, row.document_id)


if __name__ == "__main__":
    unittest.main()
