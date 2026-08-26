"""Unit tests for knowledge freshness handling."""

from __future__ import annotations

import unittest
from datetime import timedelta

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


class KnowledgeFreshnessTests(unittest.TestCase):
    def setUp(self):
        registry = KnowledgeSourceRegistry()
        self.svc = KnowledgeService(registry)
        self.scope = _scope("fresh")
        stamp = utc_now()
        self.svc.register_source(
            KnowledgeSource(
                source_id="manual.default",
                scope=self.scope,
                source_type=SOURCE_MANUAL_REFERENCE,
                name="Manual",
                trust_level=TRUST_OPERATOR,
                refresh_policy=FreshnessPolicy(policy="ttl", ttl_seconds=1, allow_stale=True),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        self.now = utc_now()
        self.svc.ingest(
            KnowledgeIngestRequest(
                scope=self.scope,
                source_id="manual.default",
                content="ttl fact about widgets",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:ttl",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
                freshness=FreshnessPolicy(policy="ttl", ttl_seconds=1, allow_stale=True),
            ),
            now=self.now,
        )
        self.later = self.now + timedelta(seconds=5)

    def test_ttl_expire_marks_stale(self):
        rows = self.svc.retrieve(
            KnowledgeQuery(query_text="widgets", scope=self.scope, include_stale=True),
            now=self.later,
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].stale)

    def test_freshness_required_excludes_stale(self):
        rows = self.svc.retrieve(
            KnowledgeQuery(
                query_text="widgets",
                scope=self.scope,
                freshness_required=True,
                include_stale=False,
            ),
            now=self.later,
        )
        self.assertEqual(rows, ())

    def test_include_stale_flags_stale_true(self):
        rows = self.svc.retrieve(
            KnowledgeQuery(query_text="widgets", scope=self.scope, include_stale=True),
            now=self.later,
        )
        self.assertTrue(rows)
        self.assertTrue(rows[0].stale)


if __name__ == "__main__":
    unittest.main()
