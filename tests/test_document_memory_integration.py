"""Unit tests for document promote_to_memory + provenance."""

from __future__ import annotations

import unittest

from documents.models import SOURCE_OPERATOR, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from memory.models import MemoryQuery, SCOPE_PROJECT, MemoryScope
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="proj-mem"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class DocumentMemoryIntegrationTests(unittest.TestCase):
    def test_promote_to_memory_preserves_provenance(self):
        scope = _scope()
        mem = MemoryService(InMemoryMemoryStore())
        svc = DocumentService(InMemoryDocumentStore(), memory_service=mem)
        row = svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename="fact.txt",
                content=b"Project fact: retention is ninety days.",
                source_type=SOURCE_OPERATOR,
                source_id="prov-1",
                sensitivity=SENSITIVITY_INTERNAL,
                promote_to_memory=True,
            )
        )
        chunks = svc.list_chunks(row.document_id, requesting_scope=scope)
        self.assertTrue(chunks)
        ch = chunks[0]
        self.assertEqual(ch.provenance_json.get("document_id"), row.document_id)
        self.assertEqual(ch.provenance_json.get("source_id"), "prov-1")

        hits = mem.retrieve(MemoryQuery(query_text="retention", scope=scope))
        self.assertTrue(hits)
        stored = mem.store.get(hits[0].memory_id)
        self.assertIsNotNone(stored)
        meta = dict(stored.metadata_safe or {})
        self.assertEqual(meta.get("document_id"), row.document_id)
        self.assertIn("chunk_id", meta)
        self.assertIn("source_location", meta)
        self.assertEqual(stored.source_type, "document")
        self.assertEqual(stored.source_ref, row.document_id)


if __name__ == "__main__":
    unittest.main()
