"""Unit tests for memory forget / tombstone behavior."""

from __future__ import annotations

import unittest

from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    STATUS_DELETED,
    MemoryIngestRequest,
    MemoryQuery,
    MemoryScope,
)
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class MemoryForgetTests(unittest.TestCase):
    def test_forget_removes_content_and_is_idempotent(self):
        store = InMemoryMemoryStore()
        svc = MemoryService(store)
        scope = _scope()
        row = svc.ingest(
            MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_SEMANTIC,
                content="forgettable fact token",
                source_type=SOURCE_OPERATOR,
                source_id="op-1",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        deleted = svc.forget(row.memory_id, requesting_scope=scope, reason="user_request")
        self.assertEqual(deleted.status, STATUS_DELETED)
        self.assertIsNone(deleted.content_safe)
        self.assertIsNone(deleted.encrypted_content)
        self.assertIsNone(svc.get(row.memory_id, requesting_scope=scope))
        hits = svc.retrieve(MemoryQuery(query_text="forgettable", scope=scope))
        self.assertFalse(any(h.memory_id == row.memory_id for h in hits))

        again = svc.forget(row.memory_id, requesting_scope=scope, reason="again")
        self.assertEqual(again.status, STATUS_DELETED)
        self.assertEqual(again.memory_id, row.memory_id)
        raw = store.get(row.memory_id)
        self.assertEqual(raw.status, STATUS_DELETED)


if __name__ == "__main__":
    unittest.main()
