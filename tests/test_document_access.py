"""Unit tests for document same-scope access policy."""

from __future__ import annotations

import unittest

from documents.models import SOURCE_OPERATOR, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="a"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class DocumentAccessTests(unittest.TestCase):
    def test_cross_scope_get_none_chunks_empty(self):
        svc = DocumentService(InMemoryDocumentStore())
        a = _scope("scope-a")
        b = _scope("scope-b")
        row = svc.ingest(
            DocumentIngestRequest(
                scope=a,
                filename="note.txt",
                content=b"private document body",
                source_type=SOURCE_OPERATOR,
                source_id="op-1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertIsNone(svc.get(row.document_id, requesting_scope=b))
        self.assertEqual(svc.list_chunks(row.document_id, requesting_scope=b), ())
        self.assertIsNotNone(svc.get(row.document_id, requesting_scope=a))
        self.assertTrue(svc.list_chunks(row.document_id, requesting_scope=a))


if __name__ == "__main__":
    unittest.main()
