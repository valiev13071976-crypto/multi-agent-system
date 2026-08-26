"""Unit tests for knowledge cross-scope access controls."""

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
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from observability.runtime import build_observability_runtime
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class KnowledgeAccessTests(unittest.TestCase):
    def test_cross_scope_retrieve_denied_no_presence_leak(self):
        obs = build_observability_runtime(env={})
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry, observability=obs)
        a = _scope("scope-alpha")
        b = _scope("scope-beta")
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
        query_text = "unique-alpha-knowledge-leak-check"
        content = "alpha-only knowledge content must not leak"
        svc.ingest(
            KnowledgeIngestRequest(
                scope=a,
                source_id="manual.a",
                content=content,
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:a",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        with self.assertRaises(KnowledgeAccessDenied) as ctx:
            svc.retrieve(
                KnowledgeQuery(query_text=query_text, scope=a),
                requesting_scope=b,
            )
        self.assertEqual(ctx.exception.reason, "cross_scope_denied")

        denied = [e for e in obs.list_events() if e.event_type == "knowledge.denied"]
        self.assertEqual(len(denied), 1)
        meta = dict(denied[0].metadata_safe or {})
        blob = str(meta)
        self.assertNotIn(a.scope_id, blob)
        self.assertNotIn(content, blob)
        self.assertNotIn(query_text, blob)
        self.assertNotIn("scope_id", meta)


if __name__ == "__main__":
    unittest.main()
