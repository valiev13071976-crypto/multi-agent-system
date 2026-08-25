"""Unit tests for memory supersession lineage."""

from __future__ import annotations

import unittest

from memory.models import (
    LINK_SUPERSEDES,
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_OPERATOR,
    STATUS_SUPERSEDED,
    MemoryIngestRequest,
    MemoryQuery,
    MemoryScope,
)
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class MemorySupersessionTests(unittest.TestCase):
    def test_supersede_marks_old_and_links_lineage(self):
        store = InMemoryMemoryStore()
        svc = MemoryService(store)
        scope = _scope()
        old = svc.ingest(
            MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_SEMANTIC,
                content="old supersede fact version one",
                source_type=SOURCE_OPERATOR,
                source_id="op-old",
                sensitivity=SENSITIVITY_INTERNAL,
            )
        )
        new = svc.supersede(
            old.memory_id,
            MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_SEMANTIC,
                content="new supersede fact version two",
                source_type=SOURCE_OPERATOR,
                source_id="op-new",
                sensitivity=SENSITIVITY_INTERNAL,
            ),
            requesting_scope=scope,
        )
        refreshed = store.get(old.memory_id)
        self.assertEqual(refreshed.status, STATUS_SUPERSEDED)

        normal = svc.retrieve(
            MemoryQuery(query_text="supersede fact", scope=scope),
        )
        ids = [h.memory_id for h in normal]
        self.assertIn(new.memory_id, ids)
        self.assertNotIn(old.memory_id, ids)

        with_old = svc.retrieve(
            MemoryQuery(
                query_text="supersede fact",
                scope=scope,
                include_superseded=True,
            ),
        )
        self.assertIn(old.memory_id, [h.memory_id for h in with_old])

        links = store.list_links(new.memory_id)
        self.assertTrue(
            any(
                L.link_type == LINK_SUPERSEDES
                and L.from_memory_id == new.memory_id
                and L.to_memory_id == old.memory_id
                for L in links
            )
        )


if __name__ == "__main__":
    unittest.main()
