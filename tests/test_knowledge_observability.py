"""Unit tests for knowledge observability events and metrics."""

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
from knowledge.service import KnowledgeDenied, KnowledgeService
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from observability.runtime import build_observability_runtime
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


FORBIDDEN_META_KEYS = ("content", "query", "scope_id", "raw_url", "url")


class KnowledgeObservabilityTests(unittest.TestCase):
    def test_events_metrics_no_sensitive_metadata(self):
        obs = build_observability_runtime(env={})
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry, observability=obs)
        scope = _scope("proj-obs")
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
        content = "observability knowledge fact token"
        query_text = "observability knowledge"
        svc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.default",
                content=content,
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:obs",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        svc.retrieve(KnowledgeQuery(query_text=query_text, scope=scope))

        other = _scope("other-scope")
        try:
            svc.retrieve(
                KnowledgeQuery(query_text=query_text, scope=scope),
                requesting_scope=other,
            )
        except KnowledgeAccessDenied:
            pass

        try:
            svc.ingest(
                KnowledgeIngestRequest(
                    scope=scope,
                    source_id="manual.default",
                    content="Bearer ghp_LEAKTEST",
                    trust_level=TRUST_OPERATOR,
                    provenance_source_ref="manual:sec",
                    sensitivity=SENSITIVITY_INTERNAL,
                    validated=True,
                )
            )
        except KnowledgeDenied:
            pass

        events = obs.list_events()
        types = {e.event_type for e in events}
        self.assertIn("knowledge.ingested", types)
        self.assertIn("knowledge.retrieved", types)
        self.assertIn("knowledge.denied", types)

        for event in events:
            meta = dict(event.metadata_safe or {})
            blob = str(meta)
            for key in FORBIDDEN_META_KEYS:
                self.assertNotIn(key, meta)
            self.assertNotIn(scope.scope_id, blob)
            self.assertNotIn(content, blob)
            self.assertNotIn(query_text, blob)

        snap = obs.metrics.snapshot()
        self.assertGreaterEqual(snap.get("knowledge_ingest_total", 0), 1)
        self.assertGreaterEqual(snap.get("knowledge_retrieval_total", 0), 1)
        self.assertGreaterEqual(snap.get("knowledge_denied_total", 0), 1)

        by_label = snap.get("by_label", {})
        for counter, buckets in by_label.items():
            if not str(counter).startswith("knowledge"):
                continue
            joined = " ".join(str(k) for k in buckets.keys())
            for key in FORBIDDEN_META_KEYS:
                self.assertNotIn(f"{key}=", joined)
            self.assertNotIn(scope.scope_id, joined)


if __name__ == "__main__":
    unittest.main()
