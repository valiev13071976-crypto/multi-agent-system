"""Unit tests for knowledge.models invariants."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from knowledge.models import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_ITEM_BYTES,
    KNOWLEDGE_POLICY_VERSION,
    KNOWLEDGE_RETRIEVAL_VERSION,
    KNOWLEDGE_SCHEMA_VERSION,
    KNOWLEDGE_SOURCE_REGISTRY_VERSION,
    KNOWLEDGE_TRUST_LEVELS,
    MAX_QUERY_CHARS,
    SOURCE_MANUAL_REFERENCE,
    SOURCE_TYPES,
    STATUS_ACTIVE,
    TRUST_OPERATOR,
    TRUST_UNVERIFIED,
    FreshnessPolicy,
    KnowledgeIngestRequest,
    KnowledgeItem,
    KnowledgeProvenance,
    KnowledgeQuery,
    KnowledgeSource,
    citation_ref_for,
    content_hash_text,
    normalize_knowledge_text,
)
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _provenance(**kwargs):
    defaults = dict(
        source_id="src-1",
        source_type=SOURCE_MANUAL_REFERENCE,
        source_ref="manual:ref",
        ingested_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        trust_level=TRUST_OPERATOR,
    )
    defaults.update(kwargs)
    return KnowledgeProvenance(**defaults)


class KnowledgeModelsTests(unittest.TestCase):
    def test_scope_and_source_required(self):
        with self.assertRaises(ValueError):
            KnowledgeSource(
                source_id="",
                scope=_scope(),
                source_type=SOURCE_MANUAL_REFERENCE,
                name="Manual",
                trust_level=TRUST_OPERATOR,
            )
        with self.assertRaises(ValueError):
            KnowledgeProvenance(
                source_id="",
                source_type=SOURCE_MANUAL_REFERENCE,
                source_ref="ref",
                ingested_at=utc_now(),
            )
        with self.assertRaises(ValueError):
            KnowledgeProvenance(
                source_id="s",
                source_type=SOURCE_MANUAL_REFERENCE,
                source_ref="",
                ingested_at=utc_now(),
            )

    def test_trust_and_freshness_enums(self):
        self.assertIn(TRUST_OPERATOR, KNOWLEDGE_TRUST_LEVELS)
        self.assertIn(TRUST_UNVERIFIED, KNOWLEDGE_TRUST_LEVELS)
        with self.assertRaises(ValueError):
            KnowledgeSource(
                source_id="s1",
                scope=_scope(),
                source_type=SOURCE_MANUAL_REFERENCE,
                name="Manual",
                trust_level="fantasy",
            )
        with self.assertRaises(ValueError):
            FreshnessPolicy(policy="never_expires")

    def test_provenance_required_on_ingest(self):
        from knowledge.validator import KnowledgeValidationError, KnowledgeValidator

        req = KnowledgeIngestRequest(
            scope=_scope(),
            source_id="s1",
            content="fact",
            trust_level=TRUST_OPERATOR,
            provenance_source_ref="",
        )
        with self.assertRaises(KnowledgeValidationError) as ctx:
            KnowledgeValidator().validate_ingest(req, max_bytes=DEFAULT_MAX_ITEM_BYTES)
        self.assertEqual(ctx.exception.reason, "provenance_required")

    def test_naive_timestamps_become_utc(self):
        naive = datetime(2024, 6, 1, 12, 0, 0)
        prov = _provenance(ingested_at=naive)
        self.assertEqual(prov.ingested_at.tzinfo, timezone.utc)

        src = KnowledgeSource(
            source_id="s1",
            scope=_scope(),
            source_type=SOURCE_MANUAL_REFERENCE,
            name="Manual",
            trust_level=TRUST_OPERATOR,
            created_at=naive,
            updated_at=naive,
        )
        self.assertEqual(src.created_at.tzinfo, timezone.utc)
        self.assertEqual(src.updated_at.tzinfo, timezone.utc)

        item = KnowledgeItem(
            knowledge_id="k1",
            scope=_scope(),
            source_id="s1",
            content="content",
            content_hash="abc",
            trust_level=TRUST_OPERATOR,
            provenance=prov,
            sensitivity=SENSITIVITY_INTERNAL,
            status=STATUS_ACTIVE,
            created_at=naive,
            updated_at=naive,
        )
        self.assertEqual(item.created_at.tzinfo, timezone.utc)
        self.assertEqual(item.updated_at.tzinfo, timezone.utc)

    def test_content_bounds_and_normalization(self):
        self.assertEqual(normalize_knowledge_text("  a   b  "), "a b")
        digest = content_hash_text("  a   b  ")
        self.assertEqual(digest, content_hash_text("a b"))
        self.assertEqual(DEFAULT_MAX_ITEM_BYTES, 32_768)
        self.assertEqual(DEFAULT_MAX_CONTEXT_BYTES, 64_000)
        with self.assertRaises(ValueError):
            KnowledgeQuery(query_text="x" * (MAX_QUERY_CHARS + 1), scope=_scope())
        with self.assertRaises(ValueError):
            KnowledgeItem(
                knowledge_id="k1",
                scope=_scope(),
                source_id="s1",
                content="content",
                content_hash="abc",
                trust_level=TRUST_OPERATOR,
                provenance=_provenance(),
                sensitivity=SENSITIVITY_INTERNAL,
                status=STATUS_ACTIVE,
                created_at=utc_now(),
                updated_at=utc_now(),
                confidence=1.5,
            )

    def test_version_constants(self):
        self.assertEqual(KNOWLEDGE_SCHEMA_VERSION, 1)
        self.assertEqual(KNOWLEDGE_POLICY_VERSION, "1.0.0")
        self.assertEqual(KNOWLEDGE_RETRIEVAL_VERSION, "1.0.0")
        self.assertEqual(KNOWLEDGE_SOURCE_REGISTRY_VERSION, "1.0.0")
        self.assertIn(SOURCE_MANUAL_REFERENCE, SOURCE_TYPES)

    def test_citation_ref(self):
        self.assertEqual(citation_ref_for(knowledge_id="abc-123"), "knowledge:abc-123")
        self.assertEqual(
            citation_ref_for(document_id="d1", chunk_id="c1"),
            "document:d1#chunk:c1",
        )
        item = KnowledgeItem(
            knowledge_id="abc-123",
            scope=_scope(),
            source_id="s1",
            content="content",
            content_hash="abc",
            trust_level=TRUST_OPERATOR,
            provenance=_provenance(),
            sensitivity=SENSITIVITY_INTERNAL,
            status=STATUS_ACTIVE,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.assertEqual(item.citation_ref, "knowledge:abc-123")

    def test_knowledge_query_rejects_url_schemes(self):
        for url in (
            "https://example.com/page",
            "http://example.com/page",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    KnowledgeQuery(query_text=url, scope=_scope())
                self.assertIn("arbitrary_url_query_denied", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
