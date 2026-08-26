"""Unit tests for KnowledgeService ingest and retrieve."""

from __future__ import annotations

import unittest

from knowledge.models import (
    SOURCE_MANUAL_REFERENCE,
    TRUST_OPERATOR,
    FreshnessPolicy,
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeSource,
)
from knowledge.registry import KnowledgeSourceRegistry
from knowledge.service import KnowledgeService
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _svc_with_manual(scope=None):
    registry = KnowledgeSourceRegistry()
    svc = KnowledgeService(registry)
    scope = scope or _scope()
    stamp = utc_now()
    svc.register_source(
        KnowledgeSource(
            source_id="manual.default",
            scope=scope,
            source_type=SOURCE_MANUAL_REFERENCE,
            name="Manual",
            trust_level=TRUST_OPERATOR,
            refresh_policy=FreshnessPolicy(policy="static"),
            created_at=stamp,
            updated_at=stamp,
        )
    )
    return svc, scope


class KnowledgeServiceTests(unittest.TestCase):
    def test_ingest_and_retrieve_manual_source(self):
        svc, scope = _svc_with_manual()
        item = svc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.default",
                content="project widget retention policy",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:fixture",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        self.assertTrue(item.knowledge_id)
        self.assertEqual(item.trust_level, TRUST_OPERATOR)
        self.assertTrue(item.citation_ref.startswith("knowledge:"))

        rows = svc.retrieve(KnowledgeQuery(query_text="widget", scope=scope))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].knowledge_id, item.knowledge_id)
        self.assertEqual(rows[0].citation_ref, item.citation_ref)
        self.assertEqual(rows[0].provenance.source_id, "manual.default")


if __name__ == "__main__":
    unittest.main()
