"""Unit tests for deterministic lexical memory retrieval ranking."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from memory.models import (
    MEMORY_SEMANTIC,
    SCOPE_PROJECT,
    SOURCE_EXTERNAL,
    SOURCE_OPERATOR,
    MemoryIngestRequest,
    MemoryQuery,
    MemoryScope,
)
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from security.encryption import SENSITIVITY_INTERNAL


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class MemoryRetrievalTests(unittest.TestCase):
    def test_lexical_ranking_confidence_recency_provenance(self):
        store = InMemoryMemoryStore()
        svc = MemoryService(store)
        scope = _scope()
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Same query tokens; ranking should prefer high confidence + operator + recent.
        low = svc.ingest(
            MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_SEMANTIC,
                content="ranking token alpha",
                source_type=SOURCE_EXTERNAL,
                source_id="ext-1",
                sensitivity=SENSITIVITY_INTERNAL,
                confidence=0.1,
            ),
            now=now - timedelta(days=10),
        )
        high = svc.ingest(
            MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_SEMANTIC,
                content="ranking token beta",
                source_type=SOURCE_OPERATOR,
                source_id="op-1",
                sensitivity=SENSITIVITY_INTERNAL,
                confidence=0.95,
            ),
            now=now,
        )
        mid = svc.ingest(
            MemoryIngestRequest(
                scope=scope,
                memory_type=MEMORY_SEMANTIC,
                content="ranking token gamma",
                source_type=SOURCE_EXTERNAL,
                source_id="ext-2",
                sensitivity=SENSITIVITY_INTERNAL,
                confidence=0.6,
            ),
            now=now - timedelta(days=1),
        )

        hits = svc.retrieve(
            MemoryQuery(query_text="ranking token", scope=scope),
        )
        ids = [h.memory_id for h in hits]
        self.assertEqual(ids[0], high.memory_id)
        self.assertIn(mid.memory_id, ids)
        self.assertIn(low.memory_id, ids)
        self.assertLess(ids.index(high.memory_id), ids.index(mid.memory_id))
        self.assertLess(ids.index(mid.memory_id), ids.index(low.memory_id))
        # Deterministic: same query yields same order.
        again = svc.retrieve(MemoryQuery(query_text="ranking token", scope=scope))
        self.assertEqual([h.memory_id for h in again], ids)


if __name__ == "__main__":
    unittest.main()
