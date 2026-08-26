"""Unit tests for knowledge retrieval filters."""

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


class KnowledgeRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.registry = KnowledgeSourceRegistry()
        self.svc = KnowledgeService(self.registry)
        self.scope = _scope("proj-ret")
        stamp = utc_now()
        for sid in ("manual.a", "manual.b"):
            self.svc.register_source(
                KnowledgeSource(
                    source_id=sid,
                    scope=self.scope,
                    source_type=SOURCE_MANUAL_REFERENCE,
                    name=sid,
                    trust_level=TRUST_OPERATOR,
                    refresh_policy=FreshnessPolicy(policy="static"),
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        self.svc.ingest(
            KnowledgeIngestRequest(
                scope=self.scope,
                source_id="manual.a",
                content="alpha source widget fact",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="a",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        self.svc.ingest(
            KnowledgeIngestRequest(
                scope=self.scope,
                source_id="manual.b",
                content="beta source widget fact",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="b",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )

    def test_retrieve_filters_by_source_ids(self):
        rows = self.svc.retrieve(
            KnowledgeQuery(
                query_text="widget",
                scope=self.scope,
                source_ids=("manual.a",),
            )
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_id, "manual.a")

    def test_disabled_source_excluded(self):
        self.svc.ingest(
            KnowledgeIngestRequest(
                scope=self.scope,
                source_id="manual.a",
                content="disabled source fixture fact",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:disabled",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        self.registry.disable("manual.a")
        rows = self.svc.retrieve(
            KnowledgeQuery(query_text="disabled", scope=self.scope)
        )
        self.assertFalse(any(r.source_id == "manual.a" for r in rows))


if __name__ == "__main__":
    unittest.main()
