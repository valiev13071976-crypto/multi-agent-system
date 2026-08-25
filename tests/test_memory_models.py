"""Unit tests for memory.models invariants."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from memory.models import (
    DEFAULT_MAX_RECORD_BYTES,
    MAX_QUERY_CHARS,
    MEMORY_EPISODIC,
    MEMORY_SEMANTIC,
    MEMORY_TYPES,
    SCOPE_PROJECT,
    SENSITIVITIES,
    SOURCE_OPERATOR,
    STATUS_ACTIVE,
    MemoryIngestRequest,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    citation_ref_for,
    content_hash_for_memory,
    normalize_memory_text,
)
from security.encryption import SENSITIVITY_INTERNAL, SENSITIVITY_SECRET, SENSITIVITY_SENSITIVE


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _provenance(**kwargs):
    defaults = dict(
        source_type=SOURCE_OPERATOR,
        source_id="src-1",
        created_by_component="test",
        ingested_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return MemoryProvenance(**defaults)


class MemoryModelsTests(unittest.TestCase):
    def test_scope_id_required(self):
        with self.assertRaises(ValueError):
            MemoryScope(scope_type=SCOPE_PROJECT, scope_id="")
        with self.assertRaises(ValueError):
            MemoryScope(scope_type=SCOPE_PROJECT, scope_id="   ")
        with self.assertRaises(ValueError):
            MemoryScope(scope_type="not_a_scope", scope_id="x")

    def test_memory_type_enum(self):
        self.assertIn(MEMORY_SEMANTIC, MEMORY_TYPES)
        with self.assertRaises(ValueError):
            MemoryIngestRequest(
                scope=_scope(),
                memory_type="fantasy",
                content="x",
                source_type=SOURCE_OPERATOR,
                source_id="s",
            )

    def test_sensitivity_enum(self):
        self.assertEqual(
            set(SENSITIVITIES),
            {SENSITIVITY_INTERNAL, SENSITIVITY_SENSITIVE, SENSITIVITY_SECRET},
        )
        with self.assertRaises(ValueError):
            MemoryIngestRequest(
                scope=_scope(),
                memory_type=MEMORY_EPISODIC,
                content="x",
                source_type=SOURCE_OPERATOR,
                source_id="s",
                sensitivity="public",
            )

    def test_naive_timestamps_become_utc(self):
        naive = datetime(2024, 6, 1, 12, 0, 0)
        prov = _provenance(ingested_at=naive)
        self.assertIsNotNone(prov.ingested_at.tzinfo)
        self.assertEqual(prov.ingested_at.tzinfo, timezone.utc)

        row = MemoryRecord(
            memory_id="m1",
            memory_type=MEMORY_SEMANTIC,
            scope=_scope(),
            content_hash="abc",
            source_type=SOURCE_OPERATOR,
            source_ref="s",
            provenance=prov,
            sensitivity=SENSITIVITY_INTERNAL,
            status=STATUS_ACTIVE,
            created_at=naive,
            updated_at=naive,
            expires_at=naive,
            version=1,
        )
        self.assertEqual(row.created_at.tzinfo, timezone.utc)
        self.assertEqual(row.updated_at.tzinfo, timezone.utc)
        self.assertEqual(row.expires_at.tzinfo, timezone.utc)

    def test_content_bounds_and_normalization(self):
        self.assertEqual(normalize_memory_text("  a   b  "), "a b")
        digest = content_hash_for_memory("  a   b  ")
        self.assertEqual(digest, content_hash_for_memory("a b"))
        self.assertEqual(DEFAULT_MAX_RECORD_BYTES, 32_768)
        with self.assertRaises(ValueError):
            MemoryQuery(query_text="x" * (MAX_QUERY_CHARS + 1), scope=_scope())
        with self.assertRaises(ValueError):
            MemoryRecord(
                memory_id="m1",
                memory_type=MEMORY_SEMANTIC,
                scope=_scope(),
                content_hash="abc",
                source_type=SOURCE_OPERATOR,
                source_ref="s",
                provenance=_provenance(),
                sensitivity=SENSITIVITY_INTERNAL,
                status=STATUS_ACTIVE,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                confidence=1.5,
            )

    def test_version_fields(self):
        prov = _provenance(version=2)
        self.assertEqual(prov.version, 2)
        row = MemoryRecord(
            memory_id="m1",
            memory_type=MEMORY_SEMANTIC,
            scope=_scope(),
            content_hash="abc",
            source_type=SOURCE_OPERATOR,
            source_ref="s",
            provenance=prov,
            sensitivity=SENSITIVITY_INTERNAL,
            status=STATUS_ACTIVE,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            version=3,
        )
        self.assertEqual(row.version, 3)

    def test_citation_ref(self):
        self.assertEqual(citation_ref_for("abc-123"), "memory:abc-123")
        row = MemoryRecord(
            memory_id="abc-123",
            memory_type=MEMORY_SEMANTIC,
            scope=_scope(),
            content_hash="abc",
            source_type=SOURCE_OPERATOR,
            source_ref="s",
            provenance=_provenance(),
            sensitivity=SENSITIVITY_INTERNAL,
            status=STATUS_ACTIVE,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.assertEqual(row.citation_ref, "memory:abc-123")


if __name__ == "__main__":
    unittest.main()
