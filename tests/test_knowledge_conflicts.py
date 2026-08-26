"""Unit tests for conflicting knowledge results."""

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


class KnowledgeConflictsTests(unittest.TestCase):
    def test_conflicting_widget_index_returned_separately(self):
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry)
        scope = _scope("conflict")
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
        svc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.a",
                content="WidgetIndex is 42",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="a",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        svc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.b",
                content="WidgetIndex is 99",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="b",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        rows = svc.retrieve(
            KnowledgeQuery(query_text="WidgetIndex", scope=scope, limit=10)
        )
        texts = {r.content for r in rows}
        refs = {r.citation_ref for r in rows}
        self.assertIn("WidgetIndex is 42", texts)
        self.assertIn("WidgetIndex is 99", texts)
        self.assertGreaterEqual(len(texts), 2)
        self.assertGreaterEqual(len(refs), 2)


if __name__ == "__main__":
    unittest.main()
