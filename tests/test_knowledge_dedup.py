"""Unit tests for knowledge deduplication."""

from __future__ import annotations

import unittest

from knowledge.models import (
    SOURCE_MANUAL_REFERENCE,
    TRUST_OPERATOR,
    FreshnessPolicy,
    KnowledgeIngestRequest,
    KnowledgeSource,
)
from knowledge.registry import KnowledgeSourceRegistry
from knowledge.service import KnowledgeService
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class KnowledgeDedupTests(unittest.TestCase):
    def test_same_content_same_source_deduped(self):
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry)
        scope = _scope("dedup")
        stamp = utc_now()
        svc.register_source(
            KnowledgeSource(
                source_id="manual.a",
                scope=scope,
                source_type=SOURCE_MANUAL_REFERENCE,
                name="A",
                trust_level=TRUST_OPERATOR,
                refresh_policy=FreshnessPolicy(policy="static"),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        req = KnowledgeIngestRequest(
            scope=scope,
            source_id="manual.a",
            content="shared canonical knowledge",
            trust_level=TRUST_OPERATOR,
            provenance_source_ref="a",
            sensitivity=SENSITIVITY_INTERNAL,
            validated=True,
        )
        a = svc.ingest(req)
        b = svc.ingest(req)
        self.assertEqual(a.knowledge_id, b.knowledge_id)

    def test_same_content_distinct_sources_both_remain(self):
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry)
        scope = _scope("dedup")
        stamp = utc_now()
        for sid in ("manual.a", "manual.b"):
            svc.register_source(
                KnowledgeSource(
                    source_id=sid,
                    scope=scope,
                    source_type=SOURCE_MANUAL_REFERENCE,
                    name=sid,
                    trust_level=TRUST_OPERATOR,
                    refresh_policy=FreshnessPolicy(policy="static"),
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        content = "shared canonical knowledge"
        a = svc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.a",
                content=content,
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="a",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        b = svc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.b",
                content=content,
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="b",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        self.assertNotEqual(a.knowledge_id, b.knowledge_id)


if __name__ == "__main__":
    unittest.main()
