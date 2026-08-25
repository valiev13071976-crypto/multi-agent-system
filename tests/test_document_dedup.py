"""Unit tests for document content-hash deduplication."""

from __future__ import annotations

import unittest

from documents.models import SOURCE_OPERATOR, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import SCOPE_PROJECT, MemoryScope
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class DocumentDedupTests(unittest.TestCase):
    def test_same_hash_same_scope_one_doc(self):
        svc = DocumentService(InMemoryDocumentStore())
        scope = _scope("same")
        payload = b"identical document bytes"
        first = svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename="a.txt",
                content=payload,
                source_type=SOURCE_OPERATOR,
                source_id="d1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        second = svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename="b.txt",
                content=payload,
                source_type=SOURCE_OPERATOR,
                source_id="d2",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertEqual(first.document_id, second.document_id)

    def test_same_hash_different_scope_two_docs(self):
        svc = DocumentService(InMemoryDocumentStore())
        payload = b"shared bytes across scopes"
        a = svc.ingest(
            DocumentIngestRequest(
                scope=_scope("scope-a"),
                filename="a.txt",
                content=payload,
                source_type=SOURCE_OPERATOR,
                source_id="d1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        b = svc.ingest(
            DocumentIngestRequest(
                scope=_scope("scope-b"),
                filename="b.txt",
                content=payload,
                source_type=SOURCE_OPERATOR,
                source_id="d2",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertNotEqual(a.document_id, b.document_id)


if __name__ == "__main__":
    unittest.main()
