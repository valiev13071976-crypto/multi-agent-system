"""Unit tests for knowledge → memory integration."""

from __future__ import annotations

import unittest

from knowledge.access import KnowledgeAccessDenied
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
from memory.models import MemoryQuery, SCOPE_PROJECT, MemoryScope
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from memory.models import utc_now
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class KnowledgeMemoryIntegrationTests(unittest.TestCase):
    def test_validated_item_persists_to_memory_with_provenance(self):
        mem = MemoryService(InMemoryMemoryStore())
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry, memory_service=mem)
        scope = _scope("mem-int")
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
        item = svc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.default",
                content="memory integration retention fact",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:mem",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        self.assertIsNotNone(item.memory_id)
        self.assertTrue(item.citation_ref.startswith("memory:"))
        stored = mem.store.get(item.memory_id)
        self.assertIsNotNone(stored)
        meta = dict(stored.metadata_safe or {})
        self.assertEqual(meta.get("trust_level"), TRUST_OPERATOR)
        self.assertTrue(str(meta.get("citation_ref", "")).startswith("knowledge:"))
        self.assertEqual(meta.get("knowledge_id"), item.knowledge_id)

    def test_cross_scope_memory_retrieve_denied(self):
        mem = MemoryService(InMemoryMemoryStore())
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry, memory_service=mem)
        a = _scope("a")
        b = _scope("b")
        stamp = utc_now()
        svc.register_source(
            KnowledgeSource(
                source_id="manual.a",
                scope=a,
                source_type=SOURCE_MANUAL_REFERENCE,
                name="A",
                trust_level=TRUST_OPERATOR,
                refresh_policy=FreshnessPolicy(policy="static"),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        svc.ingest(
            KnowledgeIngestRequest(
                scope=a,
                source_id="manual.a",
                content="scope a knowledge fact",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:a",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        with self.assertRaises(KnowledgeAccessDenied):
            svc.retrieve(
                KnowledgeQuery(query_text="scope a", scope=a),
                requesting_scope=b,
            )
        with self.assertRaises(Exception):
            mem.retrieve(MemoryQuery(query_text="scope a", scope=a), requesting_scope=b)


if __name__ == "__main__":
    unittest.main()
