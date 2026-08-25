"""Unit tests for memory ingest pipeline."""

from __future__ import annotations

import unittest

from memory.models import (
    DEFAULT_MAX_RECORD_BYTES,
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    MemoryIngestRequest,
    MemoryScope,
)
from memory.service import MemoryDenied, MemoryRecordTooLarge, MemoryService
from memory.store import InMemoryMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _req(**kwargs):
    defaults = dict(
        scope=_scope(),
        memory_type=MEMORY_SEMANTIC,
        content="useful project knowledge",
        source_type=SOURCE_OPERATOR,
        source_id="op-1",
        sensitivity=SENSITIVITY_INTERNAL,
    )
    defaults.update(kwargs)
    return MemoryIngestRequest(**defaults)


class MemoryIngestionTests(unittest.TestCase):
    def test_secret_deny(self):
        svc = MemoryService(InMemoryMemoryStore())
        with self.assertRaises(MemoryDenied) as ctx:
            svc.ingest(_req(content="Authorization: Bearer sk-live-secret-token"))
        self.assertEqual(ctx.exception.reason, "secret_ingest_denied")
        self.assertEqual(svc.store.find_active(_scope()), ())

    def test_size_limit(self):
        svc = MemoryService(InMemoryMemoryStore(), max_record_bytes=64)
        with self.assertRaises(MemoryRecordTooLarge):
            svc.ingest(_req(content="x" * 200))

    def test_empty_content(self):
        svc = MemoryService(InMemoryMemoryStore())
        with self.assertRaises(MemoryDenied) as ctx:
            svc.ingest(_req(content="   "))
        self.assertEqual(ctx.exception.reason, "memory_content_empty")

    def test_successful_ingest_with_provenance(self):
        store = InMemoryMemoryStore()
        svc = MemoryService(store)
        row = svc.ingest(
            _req(
                content="canonical fact",
                source_id="op-42",
                tags=("alpha",),
                confidence=0.9,
            )
        )
        self.assertEqual(row.status, "active")
        self.assertEqual(row.content_safe, "canonical fact")
        prov = store.get_provenance(row.memory_id)
        self.assertIsNotNone(prov)
        self.assertEqual(prov.source_id, "op-42")
        self.assertEqual(prov.source_type, SOURCE_OPERATOR)
        self.assertLessEqual(len(row.content_safe.encode("utf-8")), DEFAULT_MAX_RECORD_BYTES)


if __name__ == "__main__":
    unittest.main()
