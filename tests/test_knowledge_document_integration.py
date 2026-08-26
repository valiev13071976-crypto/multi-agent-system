"""Unit tests for knowledge ↔ document integration."""

from __future__ import annotations

import unittest

from documents.models import SOURCE_OPERATOR, DocumentIngestRequest
from documents.service import DocumentService
from documents.store import InMemoryDocumentStore
from knowledge.adapters import DocumentKnowledgeAdapter
from knowledge.models import (
    SOURCE_DOCUMENT,
    TRUST_DOCUMENT,
    FreshnessPolicy,
    KnowledgeQuery,
    KnowledgeSource,
)
from knowledge.registry import KnowledgeSourceRegistry
from knowledge.service import KnowledgeService
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class KnowledgeDocumentIntegrationTests(unittest.TestCase):
    def test_document_ingest_retrieve_citation_and_rag(self):
        scope = _scope("doc-int")
        doc_svc = DocumentService(InMemoryDocumentStore())
        doc_row = doc_svc.ingest(
            DocumentIngestRequest(
                scope=scope,
                filename="note.txt",
                content=b"Hello knowledge document integration foundation.",
                source_type=SOURCE_OPERATOR,
                source_id="op-doc",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry, document_service=doc_svc)
        stamp = utc_now()
        adapter = DocumentKnowledgeAdapter(doc_svc, source_id="document.default")
        svc.register_source(
            KnowledgeSource(
                source_id="document.default",
                scope=scope,
                source_type=SOURCE_DOCUMENT,
                name="Documents",
                trust_level=TRUST_DOCUMENT,
                refresh_policy=FreshnessPolicy(policy="static"),
                created_at=stamp,
                updated_at=stamp,
            ),
            adapter=adapter,
        )
        rows = svc.retrieve(
            KnowledgeQuery(
                query_text="integration",
                scope=scope,
                source_ids=("document.default",),
            )
        )
        self.assertTrue(rows)
        self.assertTrue(rows[0].citation_ref.startswith("document:"))
        self.assertEqual(rows[0].provenance.document_id, doc_row.document_id)

        ctx = svc.build_rag_context(rows)
        self.assertTrue(ctx.items)
        self.assertTrue(ctx.policy_override_forbidden)
        self.assertTrue(any("integration" in i.content for i in ctx.items))


if __name__ == "__main__":
    unittest.main()
