"""Unit tests for memory deduplication."""

from __future__ import annotations

import unittest

from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    MemoryIngestRequest,
    MemoryScope,
)
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class MemoryDedupTests(unittest.TestCase):
    def test_same_content_same_scope_one_active(self):
        svc = MemoryService(InMemoryMemoryStore())
        scope = _scope("same")
        req = MemoryIngestRequest(
            scope=scope,
            memory_type=MEMORY_SEMANTIC,
            content="canonical project fact",
            source_type=SOURCE_OPERATOR,
            source_id="op-1",
            sensitivity=SENSITIVITY_INTERNAL,
        )
        a = svc.ingest(req)
        b = svc.ingest(req)
        self.assertEqual(a.memory_id, b.memory_id)
        self.assertEqual(len(svc.store.find_active(scope, MEMORY_SEMANTIC)), 1)

    def test_same_content_different_scope_two_active(self):
        svc = MemoryService(InMemoryMemoryStore())
        content = "shared wording across projects"
        a = svc.ingest(
            MemoryIngestRequest(
                scope=_scope("a"),
                memory_type=MEMORY_SEMANTIC,
                content=content,
                source_type=SOURCE_OPERATOR,
                source_id="op-a",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        b = svc.ingest(
            MemoryIngestRequest(
                scope=_scope("b"),
                memory_type=MEMORY_SEMANTIC,
                content=content,
                source_type=SOURCE_OPERATOR,
                source_id="op-b",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        self.assertNotEqual(a.memory_id, b.memory_id)
        self.assertEqual(len(svc.store.find_active(_scope("a"), MEMORY_SEMANTIC)), 1)
        self.assertEqual(len(svc.store.find_active(_scope("b"), MEMORY_SEMANTIC)), 1)


if __name__ == "__main__":
    unittest.main()
