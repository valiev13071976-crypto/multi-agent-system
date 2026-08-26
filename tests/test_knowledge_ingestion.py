"""Unit tests for knowledge ingest pipeline and write policy."""

from __future__ import annotations

import unittest

from knowledge.models import (
    SOURCE_MANUAL_REFERENCE,
    TRUST_OPERATOR,
    TRUST_UNVERIFIED,
    FreshnessPolicy,
    KnowledgeIngestRequest,
    KnowledgeSource,
)
from knowledge.registry import KnowledgeSourceRegistry
from knowledge.service import KnowledgeDenied, KnowledgeService
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


class KnowledgeIngestionTests(unittest.TestCase):
    def test_unverified_not_persisted(self):
        svc, scope = _svc_with_manual()
        with self.assertRaises(KnowledgeDenied) as ctx:
            svc.ingest(
                KnowledgeIngestRequest(
                    scope=scope,
                    source_id="manual.default",
                    content="unverified web snippet",
                    trust_level=TRUST_UNVERIFIED,
                    provenance_source_ref="search:1",
                    sensitivity=SENSITIVITY_INTERNAL,
                    validated=False,
                )
            )
        self.assertEqual(ctx.exception.reason, "write_policy_denied")

    def test_secret_denied(self):
        svc, scope = _svc_with_manual()
        with self.assertRaises(KnowledgeDenied) as ctx:
            svc.ingest(
                KnowledgeIngestRequest(
                    scope=scope,
                    source_id="manual.default",
                    content="Bearer ghp_SECRETTOKEN1234567890",
                    trust_level=TRUST_OPERATOR,
                    provenance_source_ref="manual:sec",
                    sensitivity=SENSITIVITY_INTERNAL,
                    validated=True,
                )
            )
        self.assertEqual(ctx.exception.reason, "secret_denied")

    def test_validated_operator_persists(self):
        svc, scope = _svc_with_manual()
        item = svc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.default",
                content="operator validated fact",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:op",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            )
        )
        self.assertEqual(item.status, "active")
        self.assertEqual(item.trust_level, TRUST_OPERATOR)
        self.assertTrue(item.provenance.source_ref)

    def test_dedup_same_content_same_source(self):
        svc, scope = _svc_with_manual()
        req = KnowledgeIngestRequest(
            scope=scope,
            source_id="manual.default",
            content="canonical knowledge fact",
            trust_level=TRUST_OPERATOR,
            provenance_source_ref="manual:dedup",
            sensitivity=SENSITIVITY_INTERNAL,
            validated=True,
        )
        a = svc.ingest(req)
        b = svc.ingest(req)
        self.assertEqual(a.knowledge_id, b.knowledge_id)


if __name__ == "__main__":
    unittest.main()
